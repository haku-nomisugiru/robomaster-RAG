"""Encode local CAD card text with FastEmbed and query persisted Chroma vectors."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from hashlib import file_digest, sha256
from importlib.metadata import version
from itertools import batched
import json
import os
from pathlib import Path
from shutil import copyfileobj
import sys
from urllib.request import urlopen

from ingest_forum import write_json
from library import ROOT, SECTIONS, connect, result_content_key
from retrieval import mechanism_text, search_fields


MODEL = "BAAI/bge-small-zh-v1.5"
MODEL_REPO = "Qdrant/bge-small-zh-v1.5"
REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
MODEL_SHA256 = "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"
MODEL_FILES = {
    "config.json": "9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3",
    "tokenizer.json": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    "tokenizer_config.json": "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
    "special_tokens_map.json": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    "model_optimized.onnx": MODEL_SHA256,
}
COLLECTION = "robomaster-cards-bge-zh-v1"
DIMENSIONS = 512
TEXT_SCHEMA = "cad-card-v1"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
STORE = Path("runtime/chroma")
SYNC_REPORT = Path("reports/VECTOR_SYNC.json")


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def model_directory(root):
    return root / "runtime/embedding-models" / f"bge-small-zh-v1.5-{REVISION[:7]}"


def prepare_model(root):
    folder = model_directory(root)
    folder.mkdir(parents=True, exist_ok=True)
    for name, expected in MODEL_FILES.items():
        target = folder / name
        if target.is_file() and digest(target) == expected:
            continue
        pending = target.with_suffix(target.suffix + ".part")
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{REVISION}/{name}?download=true"
        print(f"Fetching pinned model file {name}", file=sys.stderr, flush=True)
        with urlopen(url, timeout=90) as response, pending.open("wb") as output:
            copyfileobj(response, output, length=1024 * 1024)
        if digest(pending) != expected:
            raise ValueError(f"Downloaded {name} differs from the pinned SHA-256")
        pending.replace(target)
    return folder


def encoder(root):
    from fastembed import TextEmbedding

    folder = model_directory(root)
    for name, expected in MODEL_FILES.items():
        path = folder / name
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"Missing or changed encoder file {name}. Run vector_search.py prepare")
    return TextEmbedding(MODEL, cache_dir=str(folder.parent), specific_model_path=str(folder),
                         local_files_only=True, threads=4, providers=["CPUExecutionProvider"])


def open_collection(path, create=False):
    import chromadb
    from chromadb.config import Settings

    if not create and not (path / "chroma.sqlite3").is_file():
        raise ValueError("Vector store is missing. Run vector_search.py sync")
    client = chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))
    identity = {"model": MODEL, "model_sha256": MODEL_SHA256, "model_revision": REVISION,
                "dimensions": DIMENSIONS, "text_schema": TEXT_SCHEMA,
                "encoder_version": version("fastembed")}
    if create:
        collection = client.get_or_create_collection(
            name=COLLECTION, embedding_function=None, metadata=identity,
            configuration={"hnsw": {"space": "cosine"}})
    else:
        collection = client.get_collection(name=COLLECTION, embedding_function=None)
    if collection.metadata != identity:
        raise ValueError("The collection model or text schema differs from this encoder")
    return collection


def card_record(card):
    fields = search_fields(card)
    phrases = [card["name"], card.get("section_label"), *mechanism_text(card),
               *fields["keywords"], *fields["description"]]
    phrases += [alias["value"] for alias in card["component_aliases"]
                if alias["kind"] in {"step_definition_name", "cad_name_without_date"}]
    text = "\n".join(dict.fromkeys(phrase.strip() for phrase in phrases if phrase and phrase.strip()))
    source = card["cad_source"]
    identity = {"component_id": card["component_id"], "cad_sha256": source["sha256"],
                "cad_path": source["path"], "definition_id": source["definition_id"] or "",
                "source_url": source["source_url"], "section": card["section"] or "supplementary"}
    fingerprint = sha256(json.dumps([text, identity], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {"card": card, "text": text, "metadata": {**identity, "content_sha256": fingerprint}}


def load_cards(root):
    with closing(connect(root, read_only=True)) as db:
        return {mid: card_record(json.loads(raw)) for mid, raw in db.execute("SELECT id,json FROM modules ORDER BY id")}


def stored_metadata(collection):
    metadata = {}
    for offset in range(0, collection.count(), 500):
        page = collection.get(limit=500, offset=offset, include=["metadatas"])
        metadata.update(zip(page["ids"], page["metadatas"], strict=True))
    return metadata


def sync_collection(collection, records, model):
    if not records:
        raise ValueError("Refusing to synchronize an empty CAD index")
    current = stored_metadata(collection)
    changed = [mid for mid, record in records.items() if current.get(mid) != record["metadata"]]
    for group in batched(changed, 48):
        documents = [records[mid]["text"] for mid in group]
        vectors = [vector.tolist() for vector in model.embed(documents, batch_size=24)]
        if len(vectors) != len(group) or any(len(vector) != DIMENSIONS for vector in vectors):
            raise ValueError("Unexpected external encoder output shape")
        collection.upsert(ids=list(group), documents=documents, embeddings=vectors,
                          metadatas=[records[mid]["metadata"] for mid in group])
    obsolete = sorted(current.keys() - records.keys())
    for group in batched(obsolete, 500):
        collection.delete(ids=list(group))
    expected = {mid: record["metadata"] for mid, record in records.items()}
    if stored_metadata(collection) != expected:
        raise ValueError("Chroma does not agree with the CAD catalog after synchronization")
    return {"total": len(records), "added": sum(mid not in current for mid in changed),
            "updated": sum(mid in current for mid in changed), "removed": len(obsolete),
            "unchanged": len(records) - len(changed)}


def find_similar(collection, records, model, query, limit=5, section=None, *, candidate_ids=None):
    return query_many(collection, records, model, [query], limit, section, candidate_ids=candidate_ids)[0]


def query_many(collection, records, model, queries, limit=5, section=None, *, candidate_ids=None):
    if not queries:
        return []
    count = collection.count()
    if candidate_ids is not None:
        count = min(count, len(candidate_ids))
    if count == 0:
        return [[] for _ in queries]
    vectors = [vector.tolist() for vector in model.embed([QUERY_PREFIX + query for query in queries], batch_size=24)]
    options = {"where": {"section": section}} if section else {}
    if candidate_ids is not None:
        options["ids"] = sorted(candidate_ids)
    size = min(count, limit * 6)
    results = []
    while True:
        found = collection.query(query_embeddings=vectors, n_results=size,
                                 include=["metadatas", "distances"], **options)
        results = []
        for ids, distances, metadata in zip(found["ids"], found["distances"], found["metadatas"], strict=True):
            hits, seen = [], set()
            for mid, distance, stored in zip(ids, distances, metadata, strict=True):
                record = records.get(mid)
                if not record or record["metadata"] != stored:
                    continue
                card = record["card"]
                source = card["cad_source"]
                key = result_content_key(card)
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"id": mid, "name": card["name"], "component_id": card["component_id"],
                             "category": card["category"], "distance": distance, "cad_source": source})
                if len(hits) == limit:
                    break
            results.append(hits)
        if size == count or all(len(hits) == limit or len(ids) < size
                                for hits, ids in zip(results, found["ids"], strict=True)):
            return results
        size = min(count, size * 2)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("prepare")
    actions.add_parser("sync")
    search = actions.add_parser("search")
    search.add_argument("query")
    search.add_argument("--section", choices=list(SECTIONS))
    search.add_argument("--limit", type=int, choices=range(1, 51), default=5, metavar="1..50")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        print(prepare_model(root))
        return
    if args.action == "search" and not args.query.strip():
        parser.error("search requires nonempty query text")
    records = load_cards(root)
    collection = open_collection(root / STORE, create=args.action == "sync")
    model = encoder(root)
    if args.action == "sync":
        result = {**sync_collection(collection, records, model), "collection": COLLECTION,
                  "model": MODEL, "model_revision": REVISION, "model_sha256": MODEL_SHA256,
                  "chroma_version": version("chromadb"), "encoder_version": version("fastembed"),
                  "dimensions": DIMENSIONS, "writer_pid": os.getpid(),
                  "synced_at_utc": datetime.now(timezone.utc).isoformat()}
        write_json(root / SYNC_REPORT, result)
    else:
        result = find_similar(collection, records, model, args.query, args.limit, args.section)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
