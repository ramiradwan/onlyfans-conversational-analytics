use base64::Engine;
use sha2::{Digest, Sha256};

fn main() {
    println!("cargo:rerun-if-changed=bridge.manifest");
    println!("cargo:rerun-if-changed=../../extension/manifest.json");
    let manifest: serde_json::Value = serde_json::from_slice(
        &std::fs::read("../../extension/manifest.json").expect("extension manifest"),
    )
    .expect("valid extension manifest");
    let key = base64::engine::general_purpose::STANDARD
        .decode(manifest["key"].as_str().expect("pinned extension key"))
        .expect("valid extension key");
    let digest = Sha256::digest(key);
    let id: String = digest[..16]
        .iter()
        .flat_map(|byte| {
            [
                char::from(b'a' + (byte >> 4)),
                char::from(b'a' + (byte & 15)),
            ]
        })
        .collect();
    println!("cargo:rustc-env=BRIDGE_EXTENSION_ID={id}");
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .windows_attributes(
                tauri_build::WindowsAttributes::new().app_manifest(include_str!("bridge.manifest")),
            )
            .app_manifest(tauri_build::AppManifest::new().commands(&[
                "desktop_state",
                "desktop_update_status",
                "desktop_refresh_presentation",
                "desktop_window",
                "desktop_browser_task",
                "desktop_setup_code",
            ])),
    )
    .expect("desktop application permissions");
}
