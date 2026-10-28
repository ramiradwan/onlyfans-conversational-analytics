use sha2::{Digest, Sha256};
use std::path::PathBuf;
fn main() {
    println!("cargo:rerun-if-env-changed=LLAMA_CPP_INCLUDE");
    println!("cargo:rerun-if-changed=src/instruction_runtime.cpp");
    if std::env::var_os("CARGO_FEATURE_INSTRUCTION_TOPICS").is_none() {
        return;
    }
    assert_eq!(
        std::env::var("CARGO_CFG_TARGET_OS").unwrap(),
        "windows",
        "instruction runtime requires Windows"
    );
    let include = PathBuf::from(
        std::env::var_os("LLAMA_CPP_INCLUDE")
            .expect("LLAMA_CPP_INCLUDE must contain llama.cpp 620d2225 headers"),
    );
    for (name, digest) in [
        (
            "llama.h",
            "961430f89e2553dbcd99e3e6c59826af9ec143f05911520c74f804d986ff3cd2",
        ),
        (
            "ggml.h",
            "475beaa7e959be8c383279d9ce9d5191f37426e6b338270cfb12b26a798129d3",
        ),
        (
            "ggml-cpu.h",
            "316279e004cdeb8e6ef78599acb602bf79a8abdf897fed9fd1914808c1518c6e",
        ),
        (
            "ggml-backend.h",
            "0e5e8e1f16d11ebbf501e38bc6705d79eee819b5f0b17476533f2100ed9674e2",
        ),
        (
            "ggml-alloc.h",
            "d114067f9099c4a0e2bbc0625c676d4894d9cf0ea27168ee072c7de0e71bb51d",
        ),
        (
            "ggml-opt.h",
            "3586de1bc8a934b5c72339e2b6937b0641e8f149b512231e666f67de0736eea2",
        ),
        (
            "gguf.h",
            "e56714aab702e5ce62ee587a409643c08f7e93e8fbb77f48ef7cc85075f96fa4",
        ),
    ] {
        let path = include.join(name);
        let bytes = std::fs::read(&path).expect("pinned header missing");
        assert_eq!(
            Sha256::digest(bytes)
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>(),
            digest,
            "llama.cpp header differs: {name}"
        );
        println!("cargo:rerun-if-changed={}", path.display());
    }
    cc::Build::new()
        .cpp(true)
        .static_crt(true)
        .std("c++17")
        .include(include)
        .file("src/instruction_runtime.cpp")
        .compile("instruction_runtime");
}
