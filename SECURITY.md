# Security policy

## Reporting a problem

Use **Security → Report a vulnerability** on the project's GitHub repository when that option is available. If the button is missing, open an issue asking the maintainers to enable private reporting. Keep incident details private until a reporting channel is available. An invented example, the affected version, and the observed verification result are usually enough to start an investigation. [GitHub's reporting instructions](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)

## Operating the local tool

Run the stdio server as the OS user who owns the review. Its client can invoke every enabled tool, including signing if you configure a private key. Give a recipient only the public key and the bundle. Keep private keys and review stores in an owner-controlled directory outside the checkout, inputs, and exports. Restrict write access to the configured directories. Use a separate server process and state directory for each trust boundary.

Obtain the producer's public key or confirm its fingerprint through an independent trusted channel. Retain the verified public keys for older bundles when rotating a signing key. A valid signature authenticates the signed bytes against the key you supplied. Source accuracy and the interpretation of a study still require review.

Use the recipient's verifier before opening a received HTML report. Treat a failed signature, a changed file or a replay discrepancy as a failed handoff. For verification automation, treat CLI exit 0 as success, exit 1 as a completed negative verification, and exit 2 as a setup or input error. The verification receipt distinguishes each stage.

The `research` and `read-paper` CLI commands contact public scholarly providers. The MCP server enables these tools only with `--literature`. Search terms go to Europe PMC and PubMed. When no terms are supplied the question itself is sent. Full-text requests send the paper identifier to Europe PMC and, when a fallback is needed, NCBI PubMed Central. Passage search terms stay local. Use population-level queries and omit private clinical information or credentials. Returned text is untrusted document content and can include statements irrelevant to the user's question. Offline build, signing and verification do not contact literature providers.
