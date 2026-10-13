# LI Token Machine

The current Token Machine is a standard-library-only, offline advisory analyzer. It distinguishes model context tokens from OAuth access credentials. It does not mint, store, exchange or refresh credentials; LI's existing authorization layer owns those operations.

Use `printf '%s' 'Research the evidence and explain limitations' | python -m lofgren_intelligence.cli token-machine`. Prompts are read from UTF-8 stdin rather than command arguments. Avoid entering secrets. The command prints only metadata, never the prompt or a prompt hash, and does not persist data or call a provider.

`analyze_prompt(prompt, *, context_window=8192, reserved_output=1024, existing_context=0, tool_definitions=None)` returns schema `li.token-machine/1`. Prompt size is limited to 32,768 UTF-8 bytes. Supplied tool metadata is limited to 64 definitions, 131,072 aggregate serialized UTF-8 bytes, depth 16 and 4,096 structural nodes per definition. Context integers are bounded to 2,000,000; the host must supply its actual model limit and existing-context count. Existing context plus output reserve must leave positive input capacity.

The rough estimate is UTF-8 bytes divided by four, rounded up. It is **not an exact tokenizer count, upper bound, safe fit guarantee or billing figure**, particularly for non-English text and code. Check the intended model's tokenizer before using the result to admit a request. LI cannot observe context that an AI host does not send to it. Tool loading and dynamic discovery differ by client; the analyzer counts only supplied metadata. No claim is made that every client loads every tool into each prompt.

Intent/risk classification uses explicit English word markers and can miss cases or flag harmless text. Clarifications and recommended steps are suggestions, not execution approvals. This module does not select tools for automatic execution, grant repository access, bypass authentication, authorize writes, or repair platform permissions by itself. OAuth and server-side permission checks remain necessary for protected features; anonymous tools remain separate.

This implementation incurs no model-provider calls. Hosting quotas, client subscriptions and infrastructure costs are outside its cost measurement. It is an initial bounded component, not autonomous intelligence or proof of all-client compatibility.

Local MCP exposes read-only `li_analyze_context` with a minimized prompt and three optional budget fields. It accepts no credential, approval, command or arbitrary tool-definition inputs. The public remote trial remains disabled; this registration does not enable it.
