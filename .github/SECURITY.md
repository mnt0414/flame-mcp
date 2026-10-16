# Security policy

flame-mcp is maintained by one person in spare time. Vulnerability reports are handled on a best-effort basis.

## Supported versions

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Earlier versions | No. Update to the latest version. |

flame-mcp has been tested only with Flame 2025.2.4 and Flame 2026.2.3 on macOS.

## Reporting a vulnerability

> [!IMPORTANT]
> **Do not report a vulnerability in a public issue, pull request or discussion.** If the details become public before a fix, other users are put at risk.

Use GitHub's private vulnerability reporting.

1. Open the **Security** tab of this repository.
2. Click **Report a vulnerability**.
3. Describe the following, then submit the report.
   - What happens: the impact
   - How to reproduce it: the operation (op) and arguments you used, your settings, and your Flame and macOS versions
   - What an attacker needs: who must be able to do what
   - If you know one, a possible fix

Only the maintainer can read the report.

## Scope

These kinds of problems are treated as vulnerabilities:

- A call through MCP gets past a policy setting: `FLAME_MCP_READONLY`, `FLAME_MCP_ALLOW_CATEGORIES`, `FLAME_MCP_ALLOW_EFFECTS` or `FLAME_MCP_ENABLE_EVAL`.
- The arguments of an operation (op) change the structure of the code sent to Flame (code injection).
- A non-ASCII string reaches Flame despite the ASCII-only check.
- Another user can connect to the bridge, or replace the bridge body.
- Another user can read or change the files that flame-mcp creates.
- An undo (`journal.revert`) writes something other than what the journal recorded.

These are documented as part of the design in [docs/safety.md](../docs/safety.md), so they are not treated as vulnerabilities:

- With `eval.run` enabled (`FLAME_MCP_ENABLE_EVAL=1`), any Python can run.
- A program running as the same user who started Flame can connect to the bridge and run code.
- Text inside a project, such as clip names or metadata, steers the assistant (prompt injection), as long as the result stays within flame-mcp's policy limits.
- Problems in Flame, an MCP client or an AI service. Report them to the vendor.
- Problems in a dependency. Report them to that project, and let the maintainer know as well if flame-mcp needs to change.

If you are not sure whether something is in scope, report it. If it is out of scope, the reply explains why.

## What happens after you report

This is a personal project, so these times are targets, not promises.

| Step | Time |
| --- | --- |
| Reply that the report was received | Within 14 days |
| Decide whether it is a vulnerability, and share the plan | Within 30 days |
| Publish a fix or a workaround | Within 90 days of the decision |

- Keep the details private until a fix is ready. After the fix is released, the details are published in a GitHub security advisory (coordinated disclosure).
- If a fix is not ready within 90 days, a known workaround, such as a setting that turns the feature off, may be published first.
- There is no bug bounty.
- If you wish, you are credited in the advisory.
