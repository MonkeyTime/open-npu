# OPEN-NPU

*Open Participatory Edge Network for Neural Processing Units*

The portal publishes the API documentation at `/docs/api` and the JSON contract at `/docs/openapi.json`. To prepare a closed deployment, see the [staging guide](deploy/PILOTE.md).

The language selector on the home page, in the documentation, and in the administration console saves your preference in the browser. A link such as `/docs/api?lang=en#capsules` opens the section in the selected language. The OpenAPI contract also accepts `?lang=en` to translate titles and descriptions while preserving routes and schemas.

Use `web/locales/fr.json` as the reference for maintaining translations. HTML pages link text to the catalogue with `data-i18n`, and scripts use `t()`. Preserve parameters such as `{0}` and markers such as `__KEEP0__`, `__OPEN0__`, and `__CLOSE0__`. The portal loads only the selected catalogue.

To package OpenVINO graphs separately or test the distributed decoder, see the [signed capsule guide](docs/CAPSULES.md).

OPEN-NPU is a local prototype for volunteer computing. Research teams submit projects through the portal. The super administrator approves them and issues an API key for each project. Windows agents enroll automatically, poll the server, and run batches on Intel AI Boost with OpenVINO.

## Deploy on Ubuntu

Run `./deploy/ubuntu.sh --domain npu.example.org --admin-cidr 10.20.0.0/24` from the checkout. It installs missing dependencies, configures HTTPS, and checks that the portal is ready. See the [Ubuntu deployment guide](deploy/PILOTE.md#install-on-ubuntu) for DNS, administrator access, local testing, and redeployment.

## Use the local instance

- Portal: `http://127.0.0.1:8787/`
- Administration: `http://127.0.0.1:8787/admin`. Sign in as `superadmin` with the password in `runtime/superadmin.txt`. Keep `runtime/` private because it also contains the SQLite database and local tokens.
- Windows agent: `dist/OpenNpuAgent/OpenNpuAgent.exe`. Keep the `_internal` directory next to the executable.
- Agent log: `runtime/agent.log`.

The following scripts start the server and a development agent on the same computer. The local agent enrolls automatically on its first run.

```powershell
.\start-local.ps1
.\start-agent.ps1
```

Each script prints its process ID. Stop the relevant process with `Stop-Process -Id <PID>` or in Task Manager. Disabling a node in the administration console releases its assigned batches and suspends the agent. A running agent resumes automatically when the node is re-enabled. The scripts launch background processes for the current session.

### Install the agent on Windows

For a local trial, select **Contribute** on the home page and download the Windows x64 installer. Open it, keep the local server address, and choose **Installer et contribuer**. The node registers without an invitation and appears in the fleet as awaiting approval. An administrator enables the node before it receives work. The installer can start the agent at sign-in. Installation uses the current Windows profile and requires no administrator privileges.

For a test on the server computer, generate the package with `./new-agent-package.ps1 -Server http://127.0.0.1:8787`, then run `./install-agent.ps1`. To distribute the agent to participants, use a [signed Windows package](docs/CAPSULES.md#distribute-the-signed-windows-package).

### Choose an enrollment mode

| Mode | How a workstation joins |
|---|---|
| Public enrollment | Volunteers use **Contribute** on the instance home page. The installer asks for no invitation. The node appears in the fleet as **Awaiting approval** until an administrator enables it. |
| Invitation-only enrollment | An administrator creates a limited-use invitation under **Access → Node invitations** and distributes a private enrollment package to the selected workstations. This is the default for the Ubuntu pilot. |

Invitations remain available when public enrollment is enabled for teams that use private packages. To require invitations for every new workstation, keep public enrollment disabled.

The signed remote installer embeds the instance's HTTPS URL, for example `https://npu.example.org`. The agent saves that URL in `node.json` and uses it for enrollment, jobs, and update checks. A server IP change leaves the installer usable if DNS still points to the same instance and HTTPS remains valid. The local trial installer proposes `http://127.0.0.1:8787`, which refers to the workstation running the installer. See [enrollment on the Ubuntu pilot](deploy/PILOTE.md#configure-workstation-enrollment).

The installer uses the current Windows profile, starts the agent in the background, and launches it again at sign-in. The computer name appears in the fleet as soon as it enrolls. Diagnostic logs are available at `%LOCALAPPDATA%\OPEN-NPU\agent.log`. Use `-NoStartup` to test without launching the agent at sign-in.

The OPEN-NPU icon appears in the Windows notification area, next to the clock or under the hidden icons menu. Hover over it to see the agent's status. Right-click to enable or disable sharing, set the upload limit, open the log, or quit the agent. Pausing lets the current batch finish and keeps existing reservations. The agent cannot receive new assignments or reservations until it resumes.

A private enrollment package includes an invitation with an expiry date and a limited number of enrollments. Share that package directory with its intended participants only. Each agent creates its own identity and retains it across reinstalls. Keep `node.json` in the local profile.

Sharing settings can also require AC power and restrict contributions to a daily window in the Windows local time zone. The window can cross midnight. A batch already in progress can finish.

### Download and update the Windows agent

The home page shows the latest release published by the instance, including its version and archive SHA-256. The public ZIP contains the executable, its dependencies, and the update installer. When public registration is open, new nodes can use the graphical installer.

From the tray menu, open **Téléchargements et mises à jour…** to visit the download section of the node's instance. Extract the ZIP before running the installer. For a signed release, first verify the extracted package using `verify-agent-package.ps1` from a separately trusted checkout and the publisher certificate fingerprint obtained through a trusted channel.

Starting with 0.3.5, an installed agent checks its instance when it first runs and every 24 hours thereafter. It downloads and verifies newer releases in the background. Windows notifies you when a release is ready. Select **Installer la mise à jour…** from the tray menu and confirm to restart into the new version. The automatic workflow requires an installed Windows agent. For remote instances it requires a signed release and a publisher fingerprint saved by a verified installation or manual update.

```powershell
C:\sources\open-npu\verify-agent-package.ps1 -Package $package -PublisherSha256 $publisherFingerprint
& (Join-Path $package 'install-agent.ps1') -Update -PublisherSha256 $publisherFingerprint
```

For a local unsigned release on a loopback instance, run `.\install-agent.ps1 -Update` from the extracted directory. Updates preserve the node configuration, preferences, and enrolled capsule key. The installer runs an NPU self-test before replacing the installed agent. It records the installed release version and remembers the verified publisher fingerprint for later updates.

Publish a ZIP after building the matching agent version. Pilot deployments require a code-signing certificate and an HTTPS timestamp service.

```powershell
.\publish-agent-release.ps1 -Version 0.3.6 -AgentDirectory ".\dist\OpenNpuAgent"
.\publish-agent-release.ps1 -Version $version -AgentDirectory $buildDirectory -SignerThumbprint $signerThumbprint -TimestampServer $timestampUrl
```

The script writes the public archive and `latest.json` under `runtime/agent-releases/` by default. For a remote instance, publish on the trusted Windows packaging workstation and copy the signed release to `agent-releases/` inside the server state directory. Copy the archive before replacing `latest.json`.

To build and publish the graphical installer for a local release, run `.\build-windows-installer.ps1 -Version 0.3.6` after publishing the corresponding ZIP. The installer embeds the agent and dependencies. It contains no invitation. The unsigned installer is unavailable in pilot mode.

To open registration on a remote pilot, set `OPEN_NPU_PUBLIC_ENROLLMENT=1` and build a signed installer on the packaging workstation using the signed release ZIP made with the same certificate. Deploy both archives and the release metadata to the server state directory. The installer embeds the instance address and the publisher fingerprint. Downloaders still need to trust the Authenticode publisher.

```powershell
.\build-windows-installer.ps1 -Version 0.3.6 -Server "https://npu.example.org" -SignerThumbprint $signerThumbprint -TimestampServer $timestampUrl
```

### Manage the fleet

The administration console includes **Calculations**, **Access**, and **Supervision**. Named accounts can use TOTP and have an owner, operator, or read-only role. The built-in `superadmin` account remains available through HTTP Basic authentication for initial setup. An owner can permanently revoke a node identity. The computer then needs to enroll again.

The home page has separate forms for research workloads and hardware offers. Find hardware offers in the **Hardware contribution offers** section of `/admin/projects`. The pilot team can use the technical contact email there to arrange a compatibility test before enrolling any computers.

**Supervision** refreshes every five seconds while the tab is visible. It shows the fleet, active leases, recent events, and batches completed per minute from the time monitoring was enabled.

Use **Access** to manage invitations for private enrollment and named API keys with expiry dates and read-only or read-write permissions. Submitting or cancelling a job requires write access. Use **Calculations** to filter and page through requests, cancel a request, or retry a cancelled or failed request under a new ID. The details page for an approved project lets you pause admission and configure quotas and priority.

Search for nodes by name, ID, or accelerator. The counters filter the fleet by status, and the table shows 25 to 100 nodes per page. The console keeps your selection as you navigate or refresh. After reviewing the selection, you can disable or re-enable up to 100 nodes in one action.

The fleet table shows the last contact time and indicates which nodes are running jobs, available, offline, have never connected, or are disabled. The history shows recent actions and how many batches returned to the queue. The table refreshes automatically while the tab is visible. If the connection fails, it keeps the last successful data and displays an error.

The audit log at `/admin/history` supports filters for date range, source, action type, project, and node. It records enrollments, node enablement and disablement, project decisions and updates, key replacements, and reservation changes. It shows the affected entities and how many batches returned to the queue when nodes were disabled. Keys, tokens, and job payloads stay out of the log. Migration imports any available legacy fleet actions.

The project list at `/admin/projects` supports search by name, institution, contact, or ID. It also supports filters by status or reservation and paginated results. Open a project to update its details, review it, manage its API key, or inspect recent jobs. Replacing an API key invalidates the previous one immediately. The new key can access previously submitted jobs.

For an approved project, enter the number of nodes to reserve. Automatic selection favours groups with the same OpenVINO hardware profile, based on name, architecture, and maximum tile count. It may fill the group with different hardware profiles or older agents that report only an accelerator name. To pick a specific group, select the nodes in the fleet, choose **Assign to a project**, open the project, and choose **Reserve selection**. If the exact number is unavailable, the existing reservation stays in place.

Reserved nodes take new batches only from their assigned project. Shared nodes process jobs for projects using the shared pool. A reservation stays in effect when a node disconnects or is disabled. Releasing a reservation leaves current batches under their existing leases and makes subsequent batches available to the shared pool.

## Build the agent

Building requires Windows x64 and Python 3.12. Running the agent requires a working Intel NPU driver.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\build-agent.ps1
```

If you manage Python with the local `uv.exe`, replace the first two commands with `.\.tools\uv.exe venv --python 3.12` and `.\.tools\uv.exe pip install -r requirements.txt`.

To check the accelerator independently of the server, run the executable with `--probe` and inspect `runtime/agent.log`. OpenVINO explicitly targets the `NPU` for inference.

## Submit a computation

Create a project in the portal and approve it in the administration console. The API key appears when you approve the project and grants access to its jobs and results.

To reproduce the demonstration, set `OPEN_NPU_API_KEY` in your PowerShell session and ensure that a compatible NPU agent is available to the project. Run the script from the repository root. It submits a job, waits for completion, and verifies the result `[4, 9, 16]`.

```powershell
.\web\demo-compute.ps1 -Server "http://127.0.0.1:8787"
```

```powershell
$headers = @{ Authorization = "Bearer $env:OPEN_NPU_API_KEY" }
$body = @{ capsule = 'square-fp16-v1'; values = @(2, -3, 4) } | ConvertTo-Json -Compress
$job = Invoke-RestMethod 'http://127.0.0.1:8787/v1/jobs' -Method Post -Headers $headers -ContentType 'application/json' -Body $body
Invoke-RestMethod "http://127.0.0.1:8787/v1/jobs/$($job.id)" -Headers $headers
```

The second request can return `queued`, `running`, `failed`, `cancelled`, or `done`. On success, `values` contains the results in input order. The demo capsule accepts between 1 and 4,096 numbers in the range −200 to 200 and squares them using FP16 rounding.

## Schedule computations

The queue splits each job into batches of up to 16 values. Each compatible agent takes one batch at a time. If an agent stops responding, its batch returns to the queue when the 120-second lease expires, with up to three attempts. When a batch fails permanently, the queue cancels any remaining queued or running batches for that job. The capsule contract defines the shape, precision, and operation. The graph is built into the agent, and job requests contain only the capsule ID and input data.

### Network throughput and batch assignment

The agent measures the server's HTTP response time and estimates upload and download throughput using a 64 KiB probe in each direction, at most once every 30 minutes. On loopback, it reports only response time. If the transfer is too fast for the probe to measure, the agent leaves throughput unspecified. The fleet shows the age of the measurement. The server ignores throughput estimates older than two hours.

In the agent's sharing settings, set the upload limit in Mbps for results and probes. Changes apply to subsequent uploads without a restart. Sharing and throughput preferences persist. For unattended installation, `./install-agent.ps1 -UploadLimitMbps 2` sets a 2 Mbps limit and `-UploadLimitMbps 0` removes it. Measured throughput reflects the limit when it caps the probe. Small control messages are sent at full speed.

When a job is submitted, the server can reduce batch sizes based on the estimated network budget of eligible nodes. Before assigning a batch, it checks whether the estimated transfer time and compute margin fit within the lease. Agents can still process small batches when measurements are missing or stale. The declared upload limit remains in effect.

The v1 queue runs independent batches for the demo capsule. The v2 queue schedules and distributes stages of signed OpenVINO capsules and transfers intermediate tensors. The v2 decoder recomputes the prefix for each token. Each model must be partitioned, exported, and validated on the target hardware.

The local instance listens on loopback. A volunteer network needs an HTTPS endpoint and a way to verify node results. The local test checks the hardware and API workflow using the demo capsule.
