//! The instruction runtime executes inside the existing worker owner.
use crate::{context::Context, entity::Control};
use anyhow::{Result, bail, ensure};
#[cfg(all(windows, feature = "instruction-topics"))]
use std::ffi::{CString, c_void};
use std::{collections::BTreeMap, ffi::CStr};

pub struct Runtime {
    #[cfg(all(windows, feature = "instruction-topics"))]
    handle: std::ptr::NonNull<c_void>,
}
// Service transfers a single resident between joined operations, never concurrent calls.
unsafe impl Send for Runtime {}

#[cfg(all(windows, feature = "instruction-topics"))]
unsafe extern "C" {
    fn instruction_open(
        paths: *const *const u16,
        backend: *const i8,
        model: *const i8,
        threads: u32,
        check: extern "C" fn(*const c_void) -> bool,
        user: *const c_void,
        stage: *mut u32,
    ) -> *mut c_void;
    fn instruction_close(handle: *mut c_void);
    fn instruction_generate(
        handle: *mut c_void,
        prompt: *const u8,
        length: usize,
        grammar: *const i8,
        context: u32,
        output: u32,
        buffer: *mut u8,
        capacity: usize,
        written: *mut usize,
        check: extern "C" fn(*const c_void) -> bool,
        user: *const c_void,
    ) -> i32;
}
#[cfg(all(windows, feature = "instruction-topics"))]
extern "C" fn check(user: *const c_void) -> bool {
    // This callback is synchronous and the borrowed control outlives the native call.
    unsafe { (&*user.cast::<Control>()).check().is_ok() }
}

fn backend_path(
    context: &Context,
    runtime: &BTreeMap<String, String>,
    name: &str,
) -> Result<std::path::PathBuf> {
    let id = runtime
        .get(name)
        .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
    let path = context.artifact_path(id, "runtime")?;
    ensure!(
        path.file_name().is_some_and(|file| file == name),
        "artifact_mismatch"
    );
    Ok(path)
}

impl Runtime {
    pub fn load(
        context: &Context,
        runtime: &BTreeMap<String, String>,
        model: &str,
        threads: u32,
        control: &Control,
    ) -> Result<Self> {
        control.check()?;
        ensure!([2, 8].contains(&threads), "resource_limit");
        let backends: Vec<_> = ["ggml-cpu-x64.dll", "ggml-cpu-alderlake.dll"]
            .into_iter()
            .filter(|name| runtime.contains_key(*name))
            .collect();
        ensure!(backends.len() == 1, "artifact_mismatch");
        let names = [
            "libomp.dll",
            "ggml-base.dll",
            "ggml.dll",
            backends[0],
            "llama.dll",
        ];
        ensure!(runtime.len() == names.len(), "artifact_mismatch");
        let mut paths = Vec::new();
        let mut closure = Vec::new();
        for name in [
            "vcruntime140.dll",
            "vcruntime140_1.dll",
            "msvcp140.dll",
            "msvcp140_1.dll",
        ] {
            let files: Vec<_> = context
                .files
                .values()
                .filter(|file| {
                    file.artifact.role == "runtime"
                        && file.path.file_name().is_some_and(|n| n == name)
                })
                .collect();
            ensure!(files.len() == 1, "artifact_mismatch");
            paths.push(&files[0].path);
            closure.push(files[0].artifact.sha256.clone());
        }
        let mut directory = None;
        let mut unique = std::collections::BTreeSet::new();
        for name in names {
            let id = runtime
                .get(name)
                .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
            let file = context
                .files
                .get(id)
                .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
            ensure!(
                unique.insert(id)
                    && file.artifact.role == "runtime"
                    && file.artifact.format_id == "llama-b11669-cpu.dll.v1"
                    && file.path.file_name().is_some_and(|n| n == name),
                "artifact_mismatch"
            );
            let parent = file
                .path
                .parent()
                .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?;
            if let Some(old) = directory {
                ensure!(old == parent, "artifact_mismatch");
            }
            directory = Some(parent);
            paths.push(&file.path);
            closure.push(file.artifact.sha256.clone());
        }
        static RUNTIME: std::sync::OnceLock<Vec<String>> = std::sync::OnceLock::new();
        ensure!(
            RUNTIME.get_or_init(|| closure.clone()) == &closure,
            "incompatible"
        );
        let model = context.artifact_path(model, "weights")?;
        let backend = backend_path(context, runtime, backends[0])?;
        #[cfg(all(windows, feature = "instruction-topics"))]
        {
            use std::os::windows::ffi::OsStrExt;
            let wide: Vec<Vec<u16>> = paths
                .iter()
                .map(|p| p.as_os_str().encode_wide().chain(Some(0)).collect())
                .collect();
            let pointers: Vec<_> = wide.iter().map(|p| p.as_ptr()).collect();
            let backend = CString::new(
                backend
                    .to_str()
                    .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?,
            )?;
            let model = CString::new(
                model
                    .to_str()
                    .ok_or_else(|| anyhow::anyhow!("artifact_mismatch"))?,
            )?;
            let mut stage = 0;
            let raw = unsafe {
                instruction_open(
                    pointers.as_ptr(),
                    backend.as_ptr(),
                    model.as_ptr(),
                    threads,
                    check,
                    (control as *const Control).cast(),
                    &mut stage,
                )
            };
            if let Some(handle) = std::ptr::NonNull::new(raw) {
                let owner = Self { handle };
                control.check()?;
                return Ok(owner);
            }
            control.check()?;
            eprintln!("instruction_open_stage={stage}");
            bail!("runtime_unavailable")
        }
        #[cfg(not(all(windows, feature = "instruction-topics")))]
        {
            let _ = (paths, model, backend);
            bail!("runtime_unavailable")
        }
    }
    pub fn generate(
        &self,
        prompt: &[u8],
        grammar: &CStr,
        context: u32,
        output: u32,
        control: &Control,
    ) -> Result<Vec<u8>> {
        control.check()?;
        ensure!(
            prompt.len() <= 1048576 && context == 8192 && output == 192,
            "resource_limit"
        );
        #[cfg(all(windows, feature = "instruction-topics"))]
        {
            let mut bytes = vec![0; 65536];
            let mut written = 0;
            let status = unsafe {
                instruction_generate(
                    self.handle.as_ptr(),
                    prompt.as_ptr(),
                    prompt.len(),
                    grammar.as_ptr(),
                    context,
                    output,
                    bytes.as_mut_ptr(),
                    bytes.len(),
                    &mut written,
                    check,
                    (control as *const Control).cast(),
                )
            };
            control.check()?;
            match status {
                0 => {}
                2 => bail!("encoded_bound"),
                3 => bail!("output_bound"),
                _ => bail!("worker_failure"),
            }
            ensure!(written <= bytes.len(), "output_bound");
            bytes.truncate(written);
            Ok(bytes)
        }
        #[cfg(not(all(windows, feature = "instruction-topics")))]
        {
            let _ = grammar;
            bail!("runtime_unavailable")
        }
    }
}
impl Drop for Runtime {
    fn drop(&mut self) {
        #[cfg(all(windows, feature = "instruction-topics"))]
        unsafe {
            instruction_close(self.handle.as_ptr());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, atomic::AtomicBool};
    use std::time::{Duration, Instant};
    #[test]
    fn cancelled_and_expired_controls_refuse_before_runtime_access() {
        let c = Control {
            cancelled: Arc::new(AtomicBool::new(true)),
            deadline: Instant::now() + Duration::from_secs(10),
        };
        assert_eq!(c.check().unwrap_err().to_string(), "cancelled");
        let c = Control {
            cancelled: Arc::new(AtomicBool::new(false)),
            deadline: Instant::now() - Duration::from_secs(1),
        };
        assert_eq!(c.check().unwrap_err().to_string(), "deadline");
    }
    #[test]
    fn backend_resolver_uses_named_artifact_after_four_crt_entries() {
        use crate::context::{Artifact, BoundFile};
        use serde_json::json;
        let names = [
            "vcruntime140.dll",
            "vcruntime140_1.dll",
            "msvcp140.dll",
            "msvcp140_1.dll",
            "libomp.dll",
            "ggml-base.dll",
            "ggml.dll",
            "ggml-cpu-alderlake.dll",
            "llama.dll",
        ];
        let mut files = BTreeMap::new();
        let mut runtime = BTreeMap::new();
        let mut ordered = Vec::new();
        for (index, name) in names.into_iter().enumerate() {
            let id = format!("runtime.{index}");
            let path = std::path::PathBuf::from("C:/bound").join(name);
            if index >= 4 {
                runtime.insert(name.to_owned(), id.clone());
            }
            ordered.push(path.clone());
            files.insert(
                id.clone(),
                BoundFile {
                    artifact: Artifact {
                        artifact_id: id,
                        sha256: "a".repeat(64),
                        size_bytes: 1,
                        role: "runtime".into(),
                        format_id: "native.v1".into(),
                    },
                    _file: std::fs::File::open(std::env::current_exe().unwrap()).unwrap(),
                    path,
                },
            );
        }
        let context = Context {
            nonce: String::new(),
            profile_json: String::new(),
            profile: json!({}),
            profile_sha: String::new(),
            engine: json!({}),
            adapters: Vec::new(),
            files,
        };
        let actual = backend_path(&context, &runtime, "ggml-cpu-alderlake.dll").unwrap();
        assert_eq!(actual, ordered[7]);
        assert_ne!(actual, ordered[3]);
        runtime.insert("ggml-cpu-alderlake.dll".into(), "runtime.3".into());
        assert!(backend_path(&context, &runtime, "ggml-cpu-alderlake.dll").is_err());
    }
}
