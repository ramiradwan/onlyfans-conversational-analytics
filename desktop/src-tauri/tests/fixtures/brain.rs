//! OS process qualification only; excluded from production builds and packaging.
#![allow(dead_code)]
#[path = "../../src/bootstrap.rs"]
mod bootstrap;
#[path = "../../src/deployment.rs"]
mod deployment;
#[path = "../../src/policy.rs"]
mod policy;
#[path = "../../src/windows.rs"]
mod windows;

fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    let data = std::path::PathBuf::from(std::env::var_os("LOCAL_ANALYTICS_DATA_DIR").unwrap());
    if args.get(1).is_some_and(|arg| arg == "--supervise") {
        let image = std::env::current_exe().unwrap().canonicalize().unwrap();
        assert!(image.as_os_str().to_string_lossy().starts_with(r"\\?\"));
        let _child = windows::Brain::start(&image, &data, "test-lifetime").unwrap();
        loop {
            std::thread::sleep(std::time::Duration::from_secs(1));
        }
    }
    assert_eq!(args.len(), 2);
    assert_eq!(args[1], "--brain");
    let status: usize = std::env::var("BRAIN_BOOTSTRAP_STATUS_HANDLE")
        .unwrap()
        .parse()
        .unwrap();
    unsafe {
        use windows_sys::Win32::{Foundation::*, Storage::FileSystem::*};
        assert_eq!(GetFileType(status as _), FILE_TYPE_PIPE);
        assert_ne!(SetHandleInformation(status as _, HANDLE_FLAG_INHERIT, 0), 0);
        let frames = b"{\"schema\":\"ofca-bootstrap-status/v1\",\"seq\":1,\"phase\":\"starting\",\"reason\":null}\n{\"schema\":\"ofca-bootstrap-status/v1\",\"seq\":2,\"phase\":\"checking\",\"reason\":null}\n";
        let mut written = 0;
        assert_ne!(
            WriteFile(
                status as _,
                frames.as_ptr(),
                frames.len() as u32,
                &mut written,
                std::ptr::null_mut()
            ),
            0
        );
        assert_eq!(written as usize, frames.len());
    }
    // Qualification uses a short ordinary install path. The child must receive
    // that form, so Python resource loaders can safely join relative assets.
    assert!(!std::env::current_exe()
        .unwrap()
        .as_os_str()
        .to_string_lossy()
        .starts_with(r"\\?\"));
    // The production parent must supply usable streams even without a console.
    // A missing stdout breaks Python/Uvicorn initialization before the listener.
    unsafe {
        use windows_sys::Win32::{
            Foundation::INVALID_HANDLE_VALUE,
            Storage::FileSystem::{ReadFile, WriteFile},
            System::Console::{
                GetStdHandle, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
            },
        };
        for stream in [STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE] {
            let handle = GetStdHandle(stream);
            assert!(!handle.is_null() && handle != INVALID_HANDLE_VALUE);
            let mut transferred = 0;
            let mut bytes = *b"x";
            if stream == STD_INPUT_HANDLE {
                assert_ne!(
                    ReadFile(
                        handle,
                        bytes.as_mut_ptr(),
                        1,
                        &mut transferred,
                        std::ptr::null_mut()
                    ),
                    0
                );
                assert_eq!(transferred, 0);
            } else {
                assert_ne!(
                    WriteFile(
                        handle,
                        bytes.as_ptr(),
                        1,
                        &mut transferred,
                        std::ptr::null_mut()
                    ),
                    0
                );
                assert_eq!(transferred, 1);
            }
        }
    }
    if let Ok(code) = std::fs::read_to_string(data.join("delayed-exit")) {
        let listener = std::net::TcpListener::bind(("127.0.0.1", policy::PORT)).unwrap();
        std::fs::write(data.join("started"), std::process::id().to_string()).unwrap();
        while !data.join("close-listener").is_file() {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        drop(listener);
        std::fs::write(data.join("listener-closed"), b"closed").unwrap();
        std::thread::sleep(std::time::Duration::from_millis(500));
        std::process::exit(code.parse().unwrap());
    }
    std::fs::write(data.join("started"), std::process::id().to_string()).unwrap();
    let raw: usize = std::env::var("BRAIN_PARENT_LIFETIME_HANDLE")
        .unwrap()
        .parse()
        .unwrap();
    let mut buffer = [0u8; 1];
    let mut read = 0;
    unsafe {
        windows_sys::Win32::Storage::FileSystem::ReadFile(
            raw as _,
            buffer.as_mut_ptr(),
            1,
            &mut read,
            std::ptr::null_mut(),
        );
    }
    std::fs::write(data.join("graceful"), b"parent closed").unwrap();
}
