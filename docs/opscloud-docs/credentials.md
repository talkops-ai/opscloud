# Provider Credentials

> Set up API keys for model providers, TypeSafe Jev routing, web search, and tracing

OpsCloud needs credentials for each model provider you wish to use. The easiest method is the interactive `/auth` credential manager inside a session. For automated CI/CD and scripts, export environment variables in your shell or CI configuration.

---

## Use `/auth` (Recommended)

Open the credential manager from any interactive TUI session:

```text
/auth
```

The credential manager displays all available providers and their current status:

| Label | Meaning |
|---|---|
| `[stored]` | Key saved with `0600` permissions in `~/.opscloud/.env` |
| `[env: VARNAME]` | Key loaded from active shell environment variable |
| `[missing]` | No key detected — select the row to input credentials |

Select a provider to enter your API key. Keys are written to `~/.opscloud/.env` with strict `0600` user-only permissions and persist across sessions.

You can also specify a custom **base URL** for private API gateways, enterprise proxies, or VPC endpoints. Leave it blank to use the provider's standard endpoint.

---

## Supported Providers & Credentials

OpsCloud supports 22+ providers out of the box:

| Provider | API Key Variable | Base URL Variable | Highlights |
|---|---|---|---|
| **Anthropic** | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` | Claude 3.5 Sonnet, Claude 3.7 Sonnet Thinking, Vision |
| **OpenAI** | `OPENAI_API_KEY` | `OPENAI_BASE_URL` | GPT-4o, GPT-4o-mini, o1, o3-mini Reasoning |
| **TypeSafe AI (Jev)** | `TYPESAFE_API_KEY` / `JEV_API_KEY` | — | Jev System One router (<70ms), safety gate (<100ms), rubric grader |
| **Google GenAI** | `GEMINI_API_KEY` / `GOOGLE_API_KEY` | `GOOGLE_GEMINI_BASE_URL` | Gemini 2.0 / 2.5 Flash & Pro, Flash Thinking |
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
| **Cohere** | `COHERE_API_KEY` | `CO_API_URL` | Command R / R+ enterprise models |
| **IBM watsonx** | `WATSONX_APIKEY` | `WATSONX_URL` | Enterprise Granite and Llama |
| **HuggingFace** | `HUGGINGFACEHUB_API_TOKEN` | `HF_INFERENCE_ENDPOINT` | Dedicated Inference Endpoints |
| **LiteLLM** | `LITELLM_API_KEY` | `LITELLM_BASE_URL` | Unified proxy for internal corporate gateways |
| **xAI** | `XAI_API_KEY` | `XAI_API_BASE` | Grok 2 / Grok 3 |
| **Baseten** | `BASETEN_API_KEY` | `BASETEN_BASE_URL` | Custom deployed container models |
| **Ollama** | *(optional)* `OLLAMA_API_KEY` | `http://localhost:11434` | Fully local offline execution (no key required) |

### Auxiliary Tools
- **Web Search**: [Tavily](https://tavily.com) API key via `TAVILY_API_KEY` or `/auth`.
- **Tracing**: [LangSmith](https://smith.langchain.com) via `LANGSMITH_API_KEY` or `/auth`.

---

## How Credentials Are Picked Up (Resolution Hierarchy)

OpsCloud discovers and resolves credentials in a deterministic 5-step fallback chain:

```text
┌────────────────────────────────────────────────────────┐
│ 1. OPSCLOUD_{KEY} Shell Variable (e.g. OPSCLOUD_OPENAI_API_KEY)
├────────────────────────────────────────────────────────┤
│ 2. Standard Shell Variable (e.g. OPENAI_API_KEY, TYPESAFE_API_KEY)
├────────────────────────────────────────────────────────┤
│ 3. Project .env File (walked up from CWD to repo root) │
├────────────────────────────────────────────────────────┤
│ 4. User Global ~/.opscloud/.env (saved via /auth)       │
├────────────────────────────────────────────────────────┤
│ 5. Cloud Native Provider Chain (AWS IAM, GCP ADC, etc.)│
└────────────────────────────────────────────────────────┘
```

1. **`OPSCLOUD_{KEY}`**: Overrides everything without modifying local files or shell profiles.
2. **Standard Environment Variables**: Standard names recognized across SDKs (`ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY`).
3. **Project `.env`**: Loaded from the working directory walking up to the Git repository root. Blocked system variables (`PATH`, `SHELL`, `PYTHONPATH`) cannot be overwritten.
4. **User Global `.env` (`~/.opscloud/.env`)**: Persisted credential store written by `/auth`.
5. **Cloud Native Provider Chains**:
   - **AWS Bedrock**: Uses standard AWS SDK resolution: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`, `~/.aws/credentials`, `~/.aws/config`, ECS/EKS IAM task roles, or EC2 instance metadata.
   - **Google Vertex AI**: Uses Google Cloud Application Default Credentials (ADC) or `GOOGLE_APPLICATION_CREDENTIALS` service account files.
   - **Azure OpenAI**: Uses `AZURE_OPENAI_API_KEY` or Azure CLI token / Managed Identity.

---

## Manage Credentials via CLI

Manage keys scriptably using `opscloud auth`:

```bash
# List configured credentials and their active sources
opscloud auth list

# Set a credential interactively
opscloud auth set openai
opscloud auth set typesafe

# Remove a stored credential from ~/.opscloud/.env
opscloud auth remove openai
```

---

## Headless CI/CD Pipelines

For headless automation or CI/CD runners, export variables directly in your pipeline environment:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export TYPESAFE_API_KEY="jev-..."
export AWS_REGION="us-east-1"

# Run unattended with Auto approval mode
opscloud -p "Validate Terraform syntax and plan in staging" --quiet -y
```
