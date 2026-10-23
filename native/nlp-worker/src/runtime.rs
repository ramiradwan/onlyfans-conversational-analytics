//! One bounded CPU runtime shared by serial component sessions.
use crate::context::Context;
use anyhow::{Result, ensure};
use gliner2_rs::options::RuntimeOptions;

pub fn options(context: &Context) -> Result<RuntimeOptions> {
    static RUNTIME: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    let runtimes = context
        .files
        .values()
        .filter(|f| {
            f.artifact.role == "runtime"
                && f.path.file_name().is_some_and(|n| n == "onnxruntime.dll")
        })
        .collect::<Vec<_>>();
    ensure!(runtimes.len() == 1, "artifact_mismatch");
    let runtime = runtimes[0];
    if let Some(old) = RUNTIME.get() {
        ensure!(old == &runtime.artifact.sha256, "incompatible");
    } else {
        crate::owned::restrict_dll_directory(&runtime.path)?;
        let options = ort::environment::GlobalThreadPoolOptions::default()
            .with_intra_threads(2)?
            .with_inter_threads(1)?
            .with_spin_control(false)?;
        ensure!(
            ort::init_from(&runtime.path)?
                .with_global_thread_pool(options)
                .with_telemetry(false)
                .commit(),
            "runtime_unavailable"
        );
        RUNTIME
            .set(runtime.artifact.sha256.clone())
            .map_err(|_| anyhow::anyhow!("runtime_unavailable"))?;
    }
    Ok(RuntimeOptions::default()
        .with_intra_threads(2)
        .with_inter_threads(None))
}
