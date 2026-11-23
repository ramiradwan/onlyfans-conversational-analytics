use base64::Engine;
use sha2::{Digest, Sha256};
use std::{env, fs, path::PathBuf};

fn build_support_channels() {
    const RELEASE: &str = "BRIDGE_BUILD_CUSTOMER_RELEASE_CONFIG";
    const PROJECTION: &str = "BRIDGE_BUILD_SUPPORT_PROJECTION";
    println!("cargo:rerun-if-env-changed={RELEASE}");
    println!("cargo:rerun-if-env-changed={PROJECTION}");
    println!("cargo:rerun-if-changed=../startup-help.js");
    let channels = match (env::var_os(RELEASE), env::var_os(PROJECTION)) {
        (None, None) => serde_json::json!({}),
        (Some(release), Some(projection)) => {
            let release = PathBuf::from(release);
            let projection = PathBuf::from(projection);
            println!("cargo:rerun-if-changed={}", release.display());
            println!("cargo:rerun-if-changed={}", projection.display());
            let bytes = fs::read(release).expect("native customer release snapshot");
            let digest = format!("{:x}", Sha256::digest(bytes));
            let projection: serde_json::Value =
                serde_json::from_slice(&fs::read(projection).expect("native support projection"))
                    .expect("valid native support projection");
            assert!(
                projection["source_sha256"].as_str() == Some(digest.as_str())
                    && projection["support_channels"].is_object(),
                "native support projection does not match the release snapshot"
            );
            projection["support_channels"].clone()
        }
        _ => panic!("native support projection requires both build inputs"),
    };
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo output directory"));
    fs::write(out.join("support-channels.json"), channels.to_string())
        .expect("write compiled support channels");
}

fn main() {
    build_support_channels();
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
            // Reviewed command permissions are checked-in build inputs. Read
            // them directly instead of regenerating tracked source files.
            .app_manifest(tauri_build::AppManifest::new()),
    )
    .expect("desktop application permissions");
}
