"""Run an unchanged pinned CPU reference through its public extraction API."""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import random
import subprocess
import sys
import time
import psutil

REVISION = "d7c727458bf6929bc9ef5ee04e13c3f717a7c455"


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class InputLimit(Exception):
    pass


def run(source, model, recipe, inputs, output):
    if output.exists():
        raise ValueError("new output directory required")
    if subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() != REVISION:
        raise ValueError("reference source revision mismatch")
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip():
        raise ValueError("reference source is not clean")
    output.mkdir(parents=True)
    process = psutil.Process()
    process.cpu_affinity(process.cpu_affinity()[:2])
    for name, value in {"HF_HUB_OFFLINE":"1", "TRANSFORMERS_OFFLINE":"1", "HF_HUB_DISABLE_TELEMETRY":"1",
                        "DO_NOT_TRACK":"1", "TOKENIZERS_PARALLELISM":"false", "RAYON_NUM_THREADS":"1",
                        "OMP_NUM_THREADS":"2", "MKL_NUM_THREADS":"2"}.items():
        os.environ[name] = value
    def no_network(event, args):
        if event in ("socket.connect", "socket.getaddrinfo", "urllib.Request"):
            raise RuntimeError("reference_network_refused")
    sys.addaudithook(no_network)
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, str(source))
    import numpy as np
    import torch
    import gliner2
    from gliner2 import AutoExtractor, ExtractorConfig
    if not Path(gliner2.__file__).resolve().is_relative_to(source.resolve()):
        raise ValueError("reference import mismatch")
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    random.seed(1729)
    np.random.seed(1729)
    torch.manual_seed(1729)
    torch.use_deterministic_algorithms(True, warn_only=False)
    settings = json.loads(recipe.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in inputs.read_text(encoding="utf-8").splitlines()]
    files = [recipe, inputs, Path(__file__)] + [p for p in model.rglob("*") if p.is_file()]
    files += [p for p in (source / "gliner2").rglob("*.py") if p.is_file()]
    receipt = dict(source_revision=REVISION, files=[dict(path=str(p),sha256=digest(p)) for p in files],
                   versions={name:importlib.metadata.version(name) for name in
                             ("torch","transformers","tokenizers","numpy","peft","sentencepiece","safetensors")},
                   cpu_affinity=process.cpu_affinity(),intra_threads=torch.get_num_threads(),
                   inter_threads=torch.get_num_interop_threads(), dtype="float32",attention="eager",
                   language_support="caller annotation; no automatic language detection",
                   purpose="development reference, not delivered runtime memory")
    (output / "before-run.json").write_text(json.dumps(receipt,indent=2)+"\n",encoding="utf-8")
    start = time.perf_counter()
    config = ExtractorConfig.from_pretrained(str(model / "config.json"))
    config.attn_implementation = "eager"
    reference = AutoExtractor.from_pretrained(str(model), config=config, local_files_only=True,
                                             use_flashdeberta=False, word_splitter="whitespace",map_location="cpu")
    reference = reference.to(device="cpu",dtype=torch.float32).eval()
    if any(parameter.dtype != torch.float32 for parameter in reference.parameters()):
        raise ValueError("reference dtype mismatch")
    receipt["load_seconds"] = time.perf_counter()-start
    receipt["load_peak_working_set"] = process.memory_info().peak_wset
    schema = reference.create_schema().entities({x["label"]:x["definition"] for x in settings["concepts"]})
    schema = schema.classification("topics", {x["label"]:x["definition"] for x in settings["topics"]},
                                   multi_label=True,cls_threshold=settings["topic_threshold"],class_act="sigmoid")
    topic_ids = {x["label"]:x["id"] for x in settings["topics"]}
    concept_ids = {x["label"]:x["id"] for x in settings["concepts"]}
    observed = []
    def observe_encoder(module, args, kwargs):
        tensor = kwargs.get("input_ids") if "input_ids" in kwargs else args[0]
        observed.append(int(tensor.shape[-1]))
        if observed[-1] > settings.get("max_input_tokens", 16384):
            raise InputLimit()
    handle = reference.encoder.register_forward_pre_hook(observe_encoder, with_kwargs=True)
    results = []
    raw_results = []
    for row in rows:
        start = time.perf_counter()
        observed.clear()
        topics, mentions = [], []
        if not row["supported"]:
            outcome = "unsupported"
        elif (len(row["text"]) > settings.get("max_source_codepoints",4096)
              or len(row["text"].split()) > settings.get("max_source_words",256)):
            outcome = "partial"
        else:
            try:
                with torch.inference_mode():
                    result = reference.extract(row["text"],schema,threshold=settings["mention_threshold"],
                                               include_confidence=True,include_spans=True,max_len=4096)
                raw_results.append(dict(id=row["id"], result=result))
                (output / "raw-results.json").write_text(json.dumps(raw_results,indent=2)+"\n",encoding="utf-8")
                for value in result.get("topics",[]):
                    topics.append(dict(topic_id=topic_ids[value["label"]],score=value["confidence"]))
                for name, values in result.get("entities",{}).items():
                    for value in values:
                        mentions.append(dict(concept_id=concept_ids[name],start=value["start"],
                                             end=value["end"],score=value["confidence"]))
                outcome = "assessed"
            except InputLimit:
                outcome = "partial"
        results.append(dict(id=row["id"],topics=topics,mentions=mentions,outcome=outcome,
                            input_tokens=observed[-1] if observed else 0,
                            elapsed_ms=round((time.perf_counter()-start)*1000),
                            peak_working_set=process.memory_info().peak_wset))
        (output / "results.json").write_text(json.dumps(results,indent=2)+"\n",encoding="utf-8")
    handle.remove()
    receipt.update(exit_code=0,completed_ids=[x["id"] for x in results],
                   final_peak_working_set=process.memory_info().peak_wset)
    (output / "receipt.json").write_text(json.dumps(receipt,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(dict(exit_code=0,completed=len(results),load_seconds=receipt["load_seconds"],
                         peak_working_set=receipt["final_peak_working_set"])))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("source","model","recipe","inputs","output"):
        parser.add_argument("--"+name,type=Path,required=True)
    args = parser.parse_args()
    output_existed = args.output.exists()
    try:
        run(**vars(args))
    except Exception:
        if not output_existed and args.output.is_dir():
            (args.output / "failure.json").write_text(json.dumps({"status": "failed", "code": "reference_probe_failed"}) + "\n", encoding="utf-8")
        print("reference_probe_failed",file=sys.stderr)
        raise SystemExit(1) from None
