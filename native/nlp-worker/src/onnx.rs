//! Inspect initializer locations before the runtime can resolve external files.
use crate::entity::Control;
use anyhow::{Result, bail, ensure};
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
};
fn varint(file: &mut File, end: u64) -> Result<u64> {
    let mut out = 0u64;
    for shift in (0..70).step_by(7) {
        ensure!(file.stream_position()? < end, "invalid_graph");
        let mut b = [0];
        file.read_exact(&mut b)?;
        ensure!(shift < 63 || b[0] <= 1, "invalid_graph");
        out |= ((b[0] & 127) as u64) << shift;
        if b[0] & 128 == 0 {
            return Ok(out);
        }
    }
    bail!("invalid_graph")
}
fn message(file: &mut File, end: u64, kind: &str, depth: usize, control: &Control) -> Result<()> {
    ensure!(depth <= 64, "invalid_graph");
    while file.stream_position()? < end {
        control.check()?;
        let key = varint(file, end)?;
        let field = key >> 3;
        ensure!(field != 0, "invalid_graph");
        match key & 7 {
            0 => {
                let n = varint(file, end)?;
                ensure!(
                    !(kind == "tensor" && (field == 13 || field == 14 && n != 0)),
                    "external_initializer"
                );
            }
            1 => {
                ensure!(
                    file.stream_position()?
                        .checked_add(8)
                        .is_some_and(|x| x <= end),
                    "invalid_graph"
                );
                file.seek(SeekFrom::Current(8))?;
            }
            5 => {
                ensure!(
                    file.stream_position()?
                        .checked_add(4)
                        .is_some_and(|x| x <= end),
                    "invalid_graph"
                );
                file.seek(SeekFrom::Current(4))?;
            }
            2 => {
                let len = varint(file, end)?;
                let next = file
                    .stream_position()?
                    .checked_add(len)
                    .ok_or_else(|| anyhow::anyhow!("invalid_graph"))?;
                ensure!(next <= end, "invalid_graph");
                ensure!(!(kind == "tensor" && field == 13), "external_initializer");
                let child = match (kind, field) {
                    ("model", 7) => Some("graph"),
                    ("model", 25) => Some("function"),
                    ("graph", 1) | ("function", 7) => Some("node"),
                    ("graph", 5) => Some("tensor"),
                    ("graph", 15) => Some("sparse"),
                    ("node", 5) | ("function", 11) => Some("attribute"),
                    ("attribute", 5) | ("attribute", 10) | ("sparse", 1) | ("sparse", 2) => {
                        Some("tensor")
                    }
                    ("attribute", 6) | ("attribute", 11) => Some("graph"),
                    ("attribute", 22) | ("attribute", 23) => Some("sparse"),
                    _ => None,
                };
                if let Some(child) = child {
                    message(file, next, child, depth + 1, control)?;
                } else {
                    file.seek(SeekFrom::Start(next))?;
                }
            }
            _ => bail!("invalid_graph"),
        }
    }
    ensure!(file.stream_position()? == end, "invalid_graph");
    Ok(())
}
pub fn check(path: &Path, control: &Control) -> Result<()> {
    let mut file = File::open(path)?;
    let size = file.metadata()?.len();
    ensure!(size > 0, "invalid_graph");
    message(&mut file, size, "model", 0, control)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn external_fields_are_not_opaque_data() {
        let external = [0x6a, 0x00];
        let root = std::env::temp_dir().join(format!("nlp-graph-check-{}", std::process::id()));
        let mut f = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&root)
            .unwrap();
        use std::io::Write;
        f.write_all(&external).unwrap();
        f.seek(SeekFrom::Start(0)).unwrap();
        let control = Control {
            cancelled: std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
            deadline: std::time::Instant::now() + std::time::Duration::from_secs(1),
        };
        assert!(message(&mut f, 2, "tensor", 0, &control).is_err());
        drop(f);
        std::fs::remove_file(root).unwrap();
    }
}
