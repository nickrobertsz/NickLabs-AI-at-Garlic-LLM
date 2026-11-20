# Large models: what the consoles can hold, and what splitting needs

## What the author measured

* The console GPU engine keeps **all** model weights in the console's GPU memory (the app reserves about 4.45 GB of direct memory; about 4.6 GB of weights fit). Only the embedding rows are read from disk, one row per token.
* Qwen3.5 4-bit file sizes (this format, about 0.56 bytes per weight):

| Model | weights file | fits one console? |
|---|---|---|
| Qwen3.5-4B | 2.37 GB | yes (tested, ~40 tok/s) |
| Qwen3.5-9B | ~5.2 GB as plain 4-bit; 4.86 GB in the mixed 4/3-bit build shipped with the app (+ speed-up head) | the shipped build yes; a plain 4-bit conversion by you is too big |
| Qwen3.5-27B | **~14.4 GB** (Ollama's own Q4_K_M download is 16.5 GB) | **no** |

* The hub checks a model's size before converting and refuses it with this explanation.

## "Can a larger model be split between two PS4s?"

* **Ollama itself cannot split one model across two computers.** It can spread a model over several GPUs inside one machine. Splitting across machines exists in llama.cpp's *RPC* mode, not in Ollama.
* **This engine cannot do it yet either.** It would need *pipeline parallelism*: console 1 runs layers 1-16 and sends the activations (a few KB per token) over the network to console 2 for layers 17-32, and so on. That is a change inside the GPU engine
  (partial model loading by layer range, activation transport, scheduling), not something the hub can add from outside.
* What it would take for 27B: about 14.4 GB / 4.6 GB per console = **at least 4 consoles** for the weights alone (plus KV-cache room), each holding about 16 layers of the 64.
  A single token would cross all four consoles in sequence, so speed would be roughly that of one console divided by the number of stages plus network latency, i.e. a few tokens per second at best. It works as a way to run models that otherwise do not run at all, not as a speed-up.
* Streaming weights from disk instead (so that not all weights are in memory at once) would need the engine to read each layer per token: 14 GB per token from a PS4 hard disk is minutes per token. Not practical.

## Roadmap if you want to try

1. Engine: load a contiguous range of layers (`model.idx` already lists every tensor with its offset: loading a slice is a matter of allocating and reading that range).
2. Engine/app: a "stage" mode: receive hidden states over TCP, run the layers, return the hidden states.
3. Hub: chain the stages of one model, and treat the chain as one "virtual console" in the scheduler (a chain speaks for one agent at a time, like a console).
4. Test with a model that fits one console first (the 4B across two consoles) and compare outputs with the single-console run: they must match.

The Ollama 27B file (`qwen3.5:27b`, 16.5 GB) was downloaded during development and is readable by the importer (it recognises the architecture and reports the size); it cannot be run until the stage mode exists.
