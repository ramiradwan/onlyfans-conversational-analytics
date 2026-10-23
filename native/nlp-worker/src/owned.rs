//! Windows handles supplied by the installed native owner, never by message paths.
use crate::{
    context::{BoundFile, Owner},
    wire,
};
use anyhow::{Result, ensure};
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::PathBuf,
};
#[cfg(windows)]
mod platform {
    use super::*;
    use std::{
        mem::zeroed,
        os::windows::{ffi::OsStringExt, io::FromRawHandle},
        ptr::null_mut,
    };
    use windows_sys::Win32::{
        Foundation::*,
        Storage::FileSystem::*,
        System::{Console::*, Pipes::*},
    };
    fn raw(n: u64) -> Result<HANDLE> {
        ensure!(
            n >= 4 && n <= usize::MAX as u64 && n != usize::MAX as u64,
            "invalid_handle"
        );
        Ok(n as usize as HANDLE)
    }
    pub fn owner(n: u64) -> Result<(Owner, Vec<BoundFile>)> {
        let handle = raw(n)?;
        ensure!(
            unsafe { GetFileType(handle) } == FILE_TYPE_PIPE,
            "invalid_owner_pipe"
        );
        unsafe { SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0) };
        let mut pipe = unsafe { File::from_raw_handle(handle) };
        let frame = wire::read_frame(&mut pipe)?;
        let mut tail = [0; 1];
        ensure!(pipe.read(&mut tail)? == 0, "invalid_owner_pipe");
        let owner: Owner = serde_json::from_value(wire::parse(&frame)?)?;
        let mut seen = std::collections::BTreeSet::new();
        let mut files = Vec::new();
        ensure!(owner.artifacts.len() <= 256, "resource_limit");
        for entry in &owner.artifacts {
            ensure!(
                entry.handle != n && seen.insert(entry.handle),
                "invalid_handle"
            );
            entry.artifact.validate()?;
            let input = raw(entry.handle)?;
            ensure!(
                unsafe { GetFileType(input) } == FILE_TYPE_DISK,
                "invalid_file"
            );
            let inherited = unsafe { File::from_raw_handle(input) };
            let reopened = unsafe {
                ReOpenFile(
                    input,
                    GENERIC_READ,
                    FILE_SHARE_READ,
                    FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
                )
            };
            ensure!(
                !reopened.is_null() && reopened != INVALID_HANDLE_VALUE,
                "invalid_file"
            );
            let mut file = unsafe { File::from_raw_handle(reopened) };
            drop(inherited);
            let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { zeroed() };
            ensure!(
                unsafe { GetFileInformationByHandle(reopened, &mut info) } != 0
                    && info.dwFileAttributes
                        & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
                        == 0,
                "invalid_file"
            );
            let size = ((info.nFileSizeHigh as u64) << 32) | info.nFileSizeLow as u64;
            ensure!(size == entry.artifact.size_bytes, "artifact_mismatch");
            let mut digest = Sha256::new();
            let mut buffer = [0; 65536];
            let mut total = 0;
            loop {
                let n = file.read(&mut buffer)?;
                if n == 0 {
                    break;
                }
                total += n as u64;
                ensure!(total <= size, "artifact_mismatch");
                digest.update(&buffer[..n]);
            }
            ensure!(
                total == size
                    && digest
                        .finalize()
                        .iter()
                        .map(|b| format!("{b:02x}"))
                        .collect::<String>()
                        == entry.artifact.sha256,
                "artifact_mismatch"
            );
            file.seek(SeekFrom::Start(0))?;
            let mut path = vec![0u16; 32768];
            let length = unsafe {
                GetFinalPathNameByHandleW(
                    reopened,
                    path.as_mut_ptr(),
                    path.len() as u32,
                    FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
                )
            };
            ensure!(length > 0 && (length as usize) < path.len(), "invalid_file");
            let path = PathBuf::from(std::ffi::OsString::from_wide(&path[..length as usize]));
            files.push(BoundFile {
                artifact: entry.artifact.clone(),
                _file: file,
                path,
            });
        }
        let exe = std::env::current_exe()?.canonicalize()?;
        ensure!(
            files.iter().any(|f| f.artifact.role == "engine"
                && f.path.canonicalize().ok().as_ref() == Some(&exe)
                && owner.engine["engine_build_sha256"] == f.artifact.sha256),
            "engine_mismatch"
        );
        Ok((owner, files))
    }
    pub struct Input {
        handle: HANDLE,
        buffer: Vec<u8>,
    }
    impl Input {
        pub fn new() -> Result<Self> {
            let handle = unsafe { GetStdHandle(STD_INPUT_HANDLE) };
            let output = unsafe { GetStdHandle(STD_OUTPUT_HANDLE) };
            ensure!(
                handle != output
                    && unsafe { GetFileType(handle) } == FILE_TYPE_PIPE
                    && unsafe { GetFileType(output) } == FILE_TYPE_PIPE,
                "private_pipes_required"
            );
            Ok(Self {
                handle,
                buffer: Vec::new(),
            })
        }
        pub fn poll(&mut self) -> Result<Option<Vec<u8>>> {
            let mut available = 0;
            ensure!(
                unsafe {
                    PeekNamedPipe(
                        self.handle,
                        null_mut(),
                        0,
                        null_mut(),
                        &mut available,
                        null_mut(),
                    )
                } != 0,
                "io_failure"
            );
            if available > 0 {
                let mut chunk = vec![0u8; (available as usize).min(65536)];
                let mut count = 0;
                ensure!(
                    unsafe {
                        windows_sys::Win32::Storage::FileSystem::ReadFile(
                            self.handle,
                            chunk.as_mut_ptr().cast(),
                            chunk.len() as u32,
                            &mut count,
                            null_mut(),
                        )
                    } != 0
                        && count > 0,
                    "io_failure"
                );
                self.buffer.extend_from_slice(&chunk[..count as usize]);
            }
            ensure!(
                self.buffer.len() <= wire::MAX_FRAME + 65540,
                "resource_limit"
            );
            if self.buffer.len() < 4 {
                return Ok(None);
            }
            let size = u32::from_le_bytes(self.buffer[..4].try_into()?) as usize;
            ensure!((1..=wire::MAX_FRAME).contains(&size), "invalid_frame");
            if self.buffer.len() < size + 4 {
                return Ok(None);
            }
            let body = self.buffer[4..size + 4].to_vec();
            self.buffer.drain(..size + 4);
            Ok(Some(body))
        }
    }
    pub fn restrict_dll_directory(path: &std::path::Path) -> Result<()> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::System::LibraryLoader::*;
        let directory = path
            .parent()
            .ok_or_else(|| anyhow::anyhow!("runtime_unavailable"))?;
        let name = directory
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>();
        ensure!(
            unsafe {
                SetDefaultDllDirectories(
                    LOAD_LIBRARY_SEARCH_SYSTEM32 | LOAD_LIBRARY_SEARCH_USER_DIRS,
                )
            } != 0
                && !unsafe { AddDllDirectory(name.as_ptr()) }.is_null(),
            "runtime_unavailable"
        );
        Ok(())
    }
}
#[cfg(windows)]
pub use platform::*;
#[cfg(not(windows))]
pub fn owner(_: u64) -> Result<(Owner, Vec<BoundFile>)> {
    anyhow::bail!("windows_owner_required")
}
#[cfg(not(windows))]
pub struct Input;
#[cfg(not(windows))]
impl Input {
    pub fn new() -> Result<Self> {
        anyhow::bail!("windows_owner_required")
    }
    pub fn poll(&mut self) -> Result<Option<Vec<u8>>> {
        anyhow::bail!("windows_owner_required")
    }
}
#[cfg(not(windows))]
pub fn restrict_dll_directory(_: &std::path::Path) -> Result<()> {
    anyhow::bail!("windows_owner_required")
}
