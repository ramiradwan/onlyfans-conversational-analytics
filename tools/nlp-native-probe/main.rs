use std::{collections::BTreeMap, io::{self, BufRead, Write}, path::Path, time::Instant};
use anyhow::{Context, Result, ensure};
use gliner2_rs::{boundary::BoundaryPipeline, RuntimeOptions,
    classification::{ClassAct, FormattedClassification},
    entities::{FormattedEntitySpan, FormattedEntityValue},
    schema_spec::{SchemaBuilder, ClassificationOptions}};
use serde::Deserialize;
use serde_json::{json, Value};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Label { id: String, label: String, definition: String }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Recipe { topics: Vec<Label>, concepts: Vec<Label>, topic_threshold: f32, mention_threshold: f32,
    max_source_codepoints: usize, max_source_words: usize, max_input_tokens: usize }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input { id: String, text: String, supported: bool }

#[cfg(windows)]
#[repr(C)]
#[derive(Default)]
struct MemoryCounters { cb: u32, faults: u32, peak_working_set: usize, working_set: usize,
    quota_peak_paged: usize, quota_paged: usize, quota_peak_nonpaged: usize,
    quota_nonpaged: usize, pagefile: usize, peak_pagefile: usize }
#[cfg(windows)]
#[link(name="kernel32")]
unsafe extern "system" {
    fn GetCurrentProcess() -> *mut std::ffi::c_void;
    fn GetProcessAffinityMask(process: *mut std::ffi::c_void, process_mask: *mut usize, system_mask: *mut usize) -> i32;
    fn SetProcessAffinityMask(process: *mut std::ffi::c_void, mask: usize) -> i32;
}
#[cfg(windows)]
#[link(name="psapi")]
unsafe extern "system" {
    fn GetProcessMemoryInfo(process: *mut std::ffi::c_void, counters: *mut MemoryCounters, size: u32) -> i32;
}
#[cfg(windows)]
fn cpu_limit() -> Result<usize> {
    let (mut allowed, mut system) = (0, 0);
    ensure!(unsafe { GetProcessAffinityMask(GetCurrentProcess(), &mut allowed, &mut system) } != 0, "affinity query failed");
    let mut selected = 0usize;
    for bit in 0..usize::BITS {
        if allowed & (1usize << bit) != 0 { selected |= 1usize << bit; }
        if selected.count_ones() == 2 { break; }
    }
    ensure!(selected.count_ones() == 2 && unsafe { SetProcessAffinityMask(GetCurrentProcess(), selected) } != 0, "affinity failed");
    Ok(selected)
}
#[cfg(windows)]
fn peak_working_set() -> Result<usize> {
    let mut counters = MemoryCounters { cb: std::mem::size_of::<MemoryCounters>() as u32, ..Default::default() };
    ensure!(unsafe { GetProcessMemoryInfo(GetCurrentProcess(), &mut counters, std::mem::size_of::<MemoryCounters>() as u32) } != 0, "memory query failed");
    Ok(counters.peak_working_set)
}

fn run() -> Result<()> {
    let affinity_mask = cpu_limit()?;
    let args: Vec<_> = std::env::args().collect();
    ensure!(args.len() == 3 || (args.len() == 4 && args[3] == "--tokens-only"), "invalid arguments");
    let tokens_only = args.len() == 4;
    ensure!(std::env::var("TOKENIZERS_PARALLELISM").as_deref() == Ok("false"), "tokenizer pools must be disabled");
    let recipe: Recipe = serde_json::from_slice(&std::fs::read(&args[2])?)?;
    ensure!(!recipe.topics.is_empty() && recipe.topics.len() <= 64 && !recipe.concepts.is_empty() && recipe.concepts.len() <= 64, "invalid label counts");
    let pool = ort::environment::GlobalThreadPoolOptions::default()
        .with_intra_threads(2)?.with_inter_threads(1)?.with_spin_control(false)?;
    ensure!(ort::init().with_global_thread_pool(pool).commit(), "runtime already initialized");
    let options = RuntimeOptions::default().with_intra_threads(2);
    let start = Instant::now();
    let pipeline = BoundaryPipeline::from_dir_with_options(Path::new(&args[1]), options)?;
    let load_ms = start.elapsed().as_millis();
    let topics: Vec<_> = recipe.topics.iter().map(|x| (x.label.clone(), x.definition.clone())).collect();
    let concepts: Vec<_> = recipe.concepts.iter().map(|x| (x.label.clone(), x.definition.clone())).collect();
    let topic_ids: BTreeMap<_,_> = recipe.topics.iter().map(|x| (x.label.as_str(), x.id.as_str())).collect();
    let concept_ids: BTreeMap<_,_> = recipe.concepts.iter().map(|x| (x.label.as_str(), x.id.as_str())).collect();
    let schema = SchemaBuilder::new().entities(concepts).classification_with_options("topics", topics,
        ClassificationOptions { multi_label: true, cls_threshold: recipe.topic_threshold, class_act: ClassAct::Sigmoid, prompt: None }).build();
    println!("{}", json!({"event":"ready", "load_ms":load_ms, "intra_threads":2, "tokenizer_parallel":false, "provider":"CPUExecutionProvider", "runtime_info":ort::info(),
        "affinity_mask":affinity_mask,"cpu_arena":false,"encoded_token_limit":recipe.max_input_tokens,
        "source_words_limit":recipe.max_source_words,"source_codepoints_limit":recipe.max_source_codepoints,"peak_working_set":peak_working_set()?}));
    io::stdout().flush()?;
    for line in io::stdin().lock().lines() {
        let line = line?;
        ensure!(line.len() <= 1_048_576, "input limit");
        let input: Input = serde_json::from_str(&line)?;
        let start = Instant::now();
        let mut topics = Vec::<Value>::new();
        let mut mentions = Vec::<Value>::new();
        let outcome;
        let mut input_tokens = 0;
        if !input.supported { outcome = "unsupported"; }
        else if input.text.chars().count() > recipe.max_source_codepoints || input.text.split_whitespace().count() > recipe.max_source_words { outcome = "partial"; }
        else {
            input_tokens = pipeline.probe_input_tokens(&input.text, &schema)?;
            if tokens_only {
                println!("{}", json!({"id":input.id,"event":"token_preflight","input_tokens":input_tokens}));
                io::stdout().flush()?;
                continue;
            }
            if input_tokens > recipe.max_input_tokens {
                println!("{}", json!({"id":input.id,"topics":[],"mentions":[],"outcome":"partial","input_tokens":input_tokens,"elapsed_ms":start.elapsed().as_millis(),"peak_working_set":peak_working_set()?}));
                io::stdout().flush()?;
                continue;
            }
            let result = pipeline.extract_with_confidence_and_spans(&input.text, &schema, recipe.mention_threshold)?;
            for (_, classification) in result.classifications {
                if let FormattedClassification::MultiWithConfidence(labels) = classification {
                    for (name, score) in labels { topics.push(json!({"topic_id":topic_ids.get(name.as_str()).context("unknown topic")?,"score":score})); }
                }
            }
            for (name, value) in result.entities {
                if let FormattedEntityValue::List(spans) = value {
                    for span in spans {
                        if let FormattedEntitySpan::TextWithConfidenceAndSpans { text: _, confidence, start, end } = span {
                            ensure!(start < end && end <= input.text.len() && input.text.is_char_boundary(start) && input.text.is_char_boundary(end), "invalid source coordinates");
                            mentions.push(json!({"concept_id":concept_ids.get(name.as_str()).context("unknown concept")?,
                                "start":input.text[..start].chars().count(), "end":input.text[..end].chars().count(), "score":confidence}));
                        }
                    }
                }
            }
            outcome = "assessed";
        }
        println!("{}", json!({"id":input.id,"topics":topics,"mentions":mentions,"outcome":outcome,"elapsed_ms":start.elapsed().as_millis(), "input_tokens":input_tokens,"peak_working_set":peak_working_set()?}));
        io::stdout().flush()?;
    }
    println!("{}", json!({"event":"shutdown","peak_working_set":peak_working_set()?}));
    Ok(())
}

fn main() {
    if run().is_err() {
        eprintln!("native_probe_failed");
        std::process::exit(1);
    }
}
