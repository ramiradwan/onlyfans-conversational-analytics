#![cfg_attr(windows, windows_subsystem = "windows")]
mod context;
mod entity;
mod onnx;
mod owned;
mod service;
mod wire;
use anyhow::{Result, ensure};
fn run() -> Result<()> {
    let args = std::env::args().collect::<Vec<_>>();
    ensure!(
        args.len() == 3 && args[1] == "--owner-context-handle",
        "owner_context_required"
    );
    let handle = args[2].parse::<u64>()?;
    ensure!(handle.to_string() == args[2], "owner_context_required");
    // Tokenization is serial; session construction uses one bounded global CPU pool.
    unsafe {
        std::env::set_var("TOKENIZERS_PARALLELISM", "false");
    }
    let (owner, files) = owned::owner(handle)?;
    let context = context::Context::bind(owner, files)?;
    let mut input = owned::Input::new()?;
    let mut service = service::Service::new(context);
    let mut output = std::io::stdout().lock();
    loop {
        for (op, payload) in service.poll()? {
            wire::write_frame(&mut output, &op, payload)?;
        }
        if let Some(frame) = input.poll()? {
            let request = wire::envelope(&frame)?;
            let replies = service.dispatch(request)?;
            let close = replies
                .iter()
                .any(|(op, p)| op == "close" && p["outcome"] == "drained");
            for (op, payload) in replies {
                wire::write_frame(&mut output, &op, payload)?;
            }
            if close {
                return Ok(());
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
}
fn main() {
    if run().is_err() {
        let _ = wire::write_frame(
            &mut std::io::stdout(),
            "transport_failure",
            wire::fail("host_failure"),
        );
        std::process::exit(1);
    }
}
