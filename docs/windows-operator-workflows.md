# Windows operator workflows

The `windows-operator` worker changes external systems: Discord servers,
dashboards, third-party apps. Every such change follows one rule: plan first,
apply only what the operator approves. This page covers that workflow, Discord
organization, Super Productivity handling, and setup.

## Worker requirements

- A dedicated Discord bot and a dedicated Hermes profile (`windows-operator`).
- The `computer_use` toolset.
- A fixed, unlocked, interactive Windows session (VM console or a connected RDP
  window). A locked or disconnected session stops computer use.
- A dedicated Chrome profile with logins you sign in yourself.
- User-managed logins: the worker never types passwords or 2FA codes.
- Stop for login, MFA, payment or admin (UAC) prompts, then resume once you
  finish them.

## Plan-first workflow for any external change

Use this for any change to an outside system.

1. Audit the current state. Read it through the API where one exists, or capture
   screenshots.
2. Post the proposed changes to the operator and wait for explicit approval.
3. Apply only the approved scope. Nothing outside it.
4. Verify. Read the state back or screenshot it.
5. Report the evidence.

The worker stops at any login, MFA, payment or admin (UAC) prompt. It posts what
it hit, waits for you to complete it, then resumes.

## Discord server organization

Prefer the Discord REST tools driven from the host orchestrator (the `discord`
toolset / REST API). REST is reliable and auditable. Clicking in the Discord
desktop app through `computer_use` on the VM is a fallback, used only when no API
path exists.

Worked example — reorganizing channels and categories:

1. Audit the current structure through REST (list guild channels and
   categories).
2. Propose the new layout to the operator and wait for approval.
3. Apply the approved layout through REST (create, move, rename).
4. Read the structure back through REST.
5. Report the before and after.

## Super Productivity integration

No integration exists yet unless one is actually built. A screenshot of the app
is not an authoritative source of tasks. Read from a real data source, in this
order of preference:

1. A dedicated Hermes skill or tool, if one is built.
2. Super Productivity's export, its local app data, or any API it exposes.
3. `computer_use` reading the screen, as a last resort only.

Workflow:

1. Read tasks from the authoritative source.
2. Classify each task: can-do (automatable), needs-approval, or
   physical-or-human-only.
3. Propose a plan and wait for approval.
4. Create Kanban tasks routed to the right worker:

   ```
   hermes kanban create "<task>" --assignee coder
   hermes kanban create "<task>" --assignee browser
   hermes kanban create "<task>" --assignee windows-operator
   ```

5. Do the approved work.
6. Mark the Super Productivity task done only after you verify the result by
   reading it back.

Do not claim the integration exists. If step 1 has no authoritative source, say
so and stop.

## Setup checklist

- VM prep: Windows 11, fixed resolution, screen lock and sleep disabled, a
  dedicated Windows account.
- Tailscale: install it so worker↔host traffic stays on the tailnet.
- Discord bot: a dedicated bot and profile for the worker.
- Chrome profile: a dedicated profile; sign in to the tools yourself
  (user-managed logins).

See [Windows VM worker](windows-vm-worker.md) for the full install.

## Examples

### Grafana login pause and resume

1. The worker opens Grafana in its Chrome profile.
2. It hits the login wall, captures a screenshot, and asks the operator to log
   in and complete MFA.
3. It waits.
4. Once you finish, it resumes and captures the dashboard.
5. It reports with the screenshot.

### Discord organization

Follow the Discord section above: audit through REST, propose the layout, wait
for approval, apply through REST, read back, report.
