# Agents: several characters on one model

A campaign, a team of tools, a group of characters: each speaker is an **agent**. The hub makes sure that

* **consoles speak in parallel** (two consoles, two voices at the same moment),
* **one console speaks for one agent at a time**, and waiting agents are served **round-robin** (no agent can starve another),
* an agent that talks again keeps its console while that console still serves its model (**sticky**: the conversation stays warm in that console's cache).

## Naming an agent

Any of these (first one found wins):

| Client | How |
|---|---|
| any HTTP client | header `X-Agent-Id: narrator` |
| OpenAI SDK/CLI | the `user` field of the request |
| Anthropic / Claude CLI | `metadata.user_id` |
| Ollama clients | `options.agent` |
| a key bound to an agent | create it in Settings → *Only for this agent*; it overrides everything the client sends |

Names are 1-64 characters: letters, digits, `. _ : @ -`.

## Example: three characters on each of two consoles

```
Console 1 holds qwen3.5-4b-garlic        Console 2 holds qwen3.5-4b-garlic
  agents: narrator, innkeeper, thief       agents: wizard, dragon, bard
```
Send every request to the one hub address with the model name and the agent. The hub spreads agents over the consoles that serve the model (least busy first) and keeps each agent on its console.
While *narrator* is speaking on console 1, *innkeeper* and *thief* wait their turn there, and *wizard* may already be speaking on console 2.

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8080/v1", api_key="local")
for who in ("narrator", "innkeeper", "thief"):
    r = c.chat.completions.create(model="qwen3.5-4b-garlic", user=who,
                                  messages=[{"role": "user", "content": f"You are {who}. Say one line."}])
    print(who, "->", r.choices[0].message.content)
```
Pin an agent to a console with `qwen3.5-4b-garlic@ps4-1`. Watch it live on the **Agents** page or with `nlag agents`.

## What is (and is not) per agent

* The hub gives each agent its own turns and its own console affinity. Conversation **history** is whatever the client sends with each request (the usual stateless chat APIs).
* A console's prompt cache holds the **last** conversation only. When two agents alternate on one console, each turn re-reads that agent's prompt, so keep prompts short or give heavy agents their own console.
* Settings → *Agent routing*: `sticky` (default) or `spread` (always the least busy console).

## Ollama's own concurrency settings

Ollama's `OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_QUEUE`, `OLLAMA_MAX_LOADED_MODELS` configure a server that runs on your computer. Here the equivalent is built in and fixed by the hardware: **one request per console at a time, any number of waiting requests (up to the console's queue limit, 8 by default,
and `queue_wait_s` 600 s)**, one loaded model per console, as many consoles as you register. Request options such as `keep_alive`, `num_ctx`, `num_predict`, `temperature`, `seed`, `stop`, `think` are honoured by the console app;
unsupported sampling options are accepted and ignored (the response header `X-Native-Ignored-Options` lists them).
