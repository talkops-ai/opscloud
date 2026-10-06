# Provider credentials

> Set up API keys for model providers, web search, and tracing

OpsCloud needs credentials for each model provider you wish to use. The easiest way is the interactive `/auth` credential manager inside a session. For automated CI/CD and scripts, set environment variables instead.

## Use `/auth` (recommended)

Open the credential manager from any interactive TUI session:

```text
/auth
```

The credential manager displays all available providers and whether credentials are configured. Select a provider to add or update its key. Keys are saved to `~/.opscloud/.env` with strict `0600` read/write permissions and persist across sessions.

Each row displays the provider name with an informative status label:

| Label | Meaning |
|---|---|
| `[stored]` | Key saved securely in `~/.opscloud/.env` |
| `[env: VARNAME]` | Key currently loaded from a shell environment variable |
| `[missing]` | No key detected — select the row to input credentials |

You can also specify a custom **base URL** for private API gateways, enterprise proxies, or VPC endpoints. Leave it blank to use the provider's default endpoint.

---

## Supported providers

OpsCloud supports 22+ providers out of the box:

| Provider | API Key Variable | Base URL Variable | Highlights |
|---|---|---|---|
| **Anthropic** | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` | Claude 3.5/3.7, Extended Thinking, Vision |
| **OpenAI** | `OPENAI_API_KEY` | `OPENAI_BASE_URL` | GPT-4o, o1, o3-mini Reasoning |
| **Google GenAI** | `GOOGLE_API_KEY` | `GOOGLE_GEMINI_BASE_URL` | Gemini 2.0 / 2.5 Flash & Pro, Flash Thinking |
| **Google Vertex AI** | `GOOGLE_CLOUD_PROJECT` | ADC | Workload Identity / ADC enterprise endpoints |
| **AWS Bedrock** | `AWS_PROFILE` / IAM | `AWS_REGION` | Native IAM role and Bedrock model support |
| **Azure OpenAI** | `AZURE_OPENAI_API_KEY` | `AZURE_OPENAI_ENDPOINT` | Azure enterprise private deployments |
| **Groq** | `GROQ_API_KEY` | `GROQ_BASE_URL` | Ultra-low latency Llama 3.3, Qwen, DeepSeek |
| **DeepSeek** | `DEEPSEEK_API_KEY` | `DEEPSEEK_API_BASE` | DeepSeek V3, R1 Reasoning |
| **Together AI** | `TOGETHER_API_KEY` | `TOGETHER_API_BASE` | Open foundation model endpoints |
| **Fireworks AI** | `FIREWORKS_API_KEY` | `FIREWORKS_BASE_URL` | High-speed function calling |
| **OpenRouter** | `OPENROUTER_API_KEY` | `OPENROUTER_API_BASE` | Multi-provider routing gateway |
| **Mistral AI** | `MISTRAL_API_KEY` | `MISTRAL_BASE_URL` | Mistral Large, Codestral, Pixtral |
| **NVIDIA NIM** | `NVIDIA_API_KEY` | `NVIDIA_BASE_URL` | Accelerated NIM enterprise endpoints |
| **Perplexity** | `PPLX_API_KEY` | `PERPLEXITY_BASE_URL` | Search-augmented Sonar models |
| **Cohere** | `COHERE_API_KEY` | `CO_API_URL` | Command R / R+ |
| **IBM watsonx** | `WATSONX_APIKEY` | `WATSONX_URL` | Enterprise Granite and Llama |
| **HuggingFace** | `HUGGINGFACEHUB_API_TOKEN` | `HF_INFERENCE_ENDPOINT` | Dedicated Inference Endpoints |
| **LiteLLM** | `LITELLM_API_KEY` | `LITELLM_BASE_URL` | Unified proxy for internal corporate gateways |
| **xAI** | `XAI_API_KEY` | `XAI_API_BASE` | Grok 2 / Grok 3 |
| **Baseten** | `BASETEN_API_KEY` | `BASETEN_BASE_URL` | Custom deployed container models |
| **Ollama** | *(optional)* `OLLAMA_API_KEY` | `http://localhost:11434` | Fully local offline execution |

**Authentication notes:**
- **AWS Bedrock** authenticates using your active AWS environment (`AWS_PROFILE`, `AWS_REGION`, or ECS/EKS IAM task roles).
- **Google Vertex AI** uses Google Cloud Application Default Credentials (ADC) — no API key needed, just configure `GOOGLE_CLOUD_PROJECT`.
- **Ollama** runs locally on your workstation and does not require an API key.

---

## Key resolution order

When multiple sources define the same provider key, OpsCloud resolves them in the following order:

1. **`OPSCLOUD_{KEY}`** — Prefixed environment variable (e.g. `OPSCLOUD_OPENAI_API_KEY`) — always wins
2. **Standard environment variable** — e.g. `OPENAI_API_KEY`
3. **`~/.opscloud/.env`** — Global user dotenv file
4. **`/auth` stored credential** — Saved through the interactive manager

The `OPSCLOUD_` prefix allows you to override any key from your shell without modifying files.

---

## Manage credentials from the command line

Use `opscloud auth` for scripted workflows:

```bash
# List configured credentials and sources
opscloud auth list

# Set a credential
opscloud auth set openai

# Remove a stored credential
opscloud auth remove openai
```

---

## Environment variables for CI/CD

For headless CI/CD pipelines, export credentials directly:

```bash
export OPENAI_API_KEY="sk-..."
opscloud -n "Validate the Terraform modules" --quiet -y
```

Or maintain a `~/.opscloud/.env` file:

```env
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
TAVILY_API_KEY=tvly-...
LANGSMITH_API_KEY=ls-...
```

OpsCloud strictly blocks sensitive system environment variables (`PATH`, `HOME`, `PYTHONPATH`, `SHELL`, `TMPDIR`) from being modified via `.env` files to prevent environment hijacking.

---

## Web search credentials

OpsCloud uses [Tavily](https://tavily.com) for real-time web searches (querying CVEs, official cloud documentation, and library releases). Configure via `/auth` or:

```bash
export TAVILY_API_KEY="tvly-..."
```

---

## LangSmith tracing credentials

Enable execution tracing and subagent debugging via `/auth` or:

```bash
export LANGSMITH_API_KEY="ls-..."
export LANGSMITH_PROJECT="opscloud-production"  # optional, defaults to "opscloud"
```

Tracing initializes automatically on launch when a LangSmith API key is present.
