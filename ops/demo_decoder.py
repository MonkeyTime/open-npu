"""Crée un décodeur Transformer miniature et déterministe pour valider le pipeline."""

import argparse
import json
from pathlib import Path

import numpy as np
from openvino import Model, Type, opset13 as ov, serialize

CONTEXT = 8
HIDDEN = 16
VOCAB = 32
PADDING = 0
EOS = 31
SEED = 20261117


def weight(random, shape, scale=0.15):
    return ov.constant(random.normal(0, scale, shape).astype(np.float16))


def linear(value, matrix, random):
    return ov.matmul(value, weight(random, matrix), False, False)


def attention(value, random):
    query, key, output = [linear(value, (HIDDEN, HIDDEN), random) for _ in range(3)]
    mask = np.triu(np.full((CONTEXT, CONTEXT), -100, dtype=np.float16), k=1)
    score = ov.matmul(query, key, False, True)
    score = ov.add(ov.multiply(score, ov.constant(np.float16(HIDDEN ** -0.5))), ov.constant(mask))
    attended = ov.matmul(ov.softmax(score, -1), output, False, False)
    residual = ov.add(value, linear(attended, (HIDDEN, HIDDEN), random))
    hidden = ov.relu(linear(residual, (HIDDEN, HIDDEN * 2), random))
    return ov.add(residual, linear(hidden, (HIDDEN * 2, HIDDEN), random))


def first(tokens, random):
    embedded = ov.gather(weight(random, (VOCAB, HIDDEN)), tokens, ov.constant(np.int64(0)))
    return attention(ov.add(embedded, weight(random, (1, CONTEXT, HIDDEN))), random)


def last(hidden, random):
    return linear(attention(hidden, random), (HIDDEN, VOCAB), random)


def reference():
    random = np.random.default_rng(SEED)
    tokens = ov.parameter([1, CONTEXT], Type.i32)
    return Model([last(first(tokens, random), random)], [tokens], "tiny-decoder-reference")


def generate(directory):
    directory.mkdir(parents=True)
    random = np.random.default_rng(SEED)
    tokens = ov.parameter([1, CONTEXT], Type.i32)
    hidden = ov.parameter([1, CONTEXT, HIDDEN], Type.f16)
    stages = [Model([first(tokens, random)], [tokens], "embeddings-block-1"),
              Model([last(hidden, random)], [hidden], "block-2-logits")]
    for index, stage in enumerate(stages):
        serialize(stage, directory / f"{index}.xml", directory / f"{index}.bin")
    intermediate = {"shape": [1, CONTEXT, HIDDEN], "dtype": "f16"}
    definition = {
        "format": 1, "id": "tiny-decoder", "version": 1, "kind": "decoder", "pad": PADDING, "eos": EOS,
        "stages": [
            {"input": {"shape": [1, CONTEXT], "dtype": "i32"}, "output": intermediate},
            {"input": intermediate, "output": {"shape": [1, CONTEXT, VOCAB], "dtype": "f16"}},
        ],
    }
    (directory / "definition.json").write_text(json.dumps(definition, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    generate(parser.parse_args().directory)
