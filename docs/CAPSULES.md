# Signed NPU capsules and distributed decoding

Each stage contains a graph and a static tensor contract. The manifest identifies assets by SHA-256 and carries an Ed25519 signature. The agent verifies the signature and file integrity, then compiles the graph for the NPU in a dedicated process with a 90-second timeout per computation. The process caches one compiled stage and runs under the current Windows account. The pilot requires graphs reviewed by the trusted publisher.

| Manifest format | Stage files | Compatible agents |
| --- | --- | --- |
| 1 | `0.xml` and `0.bin`, then `1.xml` and `1.bin` for the next stage | Intel / OpenVINO |
| 2 | `0.onnx`, then `1.onnx` for the next stage | Agent 0.3.7+ on Intel / OpenVINO or compatible AMD Ryzen AI / Vitis AI |

An ONNX stage must have one static input and one static output matching its signed tensor contract. Include all weights inside the ONNX file. Validate each graph on its target NPU and driver before approval. Vitis AI may compile FP32 graphs to BF16 and supports a hardware-dependent set of operators. [AMD model support](https://ryzenai.docs.amd.com/projects/WinML/en/stable/model_support.html)

To try an ONNX tensor capsule, copy the repository's `npu-probe.onnx` to `0.onnx` inside a new bundle directory and save this `definition.json` beside it. Sign and import the bundle with the commands below, substituting its directory for `tiny-decoder`. Submit 1,024 input values to `/v2/jobs` and read the FP32 output. This model doubles each input within the probe's small integer range.

```json
{
  "format": 2,
  "id": "npu-probe",
  "version": 1,
  "kind": "tensor",
  "stages": [
    {
      "input": {"dtype": "f32", "shape": [1, 16, 8, 8]},
      "output": {"dtype": "f32", "shape": [1, 16, 8, 8]}
    }
  ]
}
```

## Set up signing trust

1. Generate a key pair on the workstation that signs capsules. Keep the private key encrypted and separate from the server and Windows packages.
2. Copy only `capsule-signing.pub` to the server's state directory (`runtime/` by default). Add the same public key to the participant package with `-CapsulePublicKeyFile`. For an existing agent, copy it next to `node.json` and restart the agent. Distribute and verify the key over a trusted channel.
3. You can then import and approve releases signed with this key independently of the agent. The pilot uses one trusted key. To rotate it, withdraw the old releases first and distribute the new public key.

```powershell
.\.venv\Scripts\python.exe -m ops.capsule_tool keygen .\capsule-private.pem .\capsule-signing.pub
```

After building agent 0.3.2, prepare a Windows package for a remote server as follows.

```powershell
.\new-agent-package.ps1 -Server "https://npu.example.org" -InvitationFile ".\invitation.json" -CapsulePublicKeyFile ".\capsule-signing.pub" -SignerThumbprint $signerThumbprint -TimestampServer $timestampUrl
```

## Create and approve a release

An exported model must provide `0.xml` and `0.bin` for stage 0, `1.xml` and `1.bin` for stage 1, and so on. The `definition.json` file describes the name, version, kind, and input and output shapes and types. The output contract of each stage must match the input contract of the next. The definition uses format `1` and accepts `f16`, `f32`, and `i32`. A signed and approved release remains bound to its digest. Increment `version` to distribute a new graph.

On the signing workstation, build and sign the miniature Transformer decoder used for validation.

```powershell
.\.venv\Scripts\python.exe -m ops.demo_decoder .\runtime\tiny-decoder
.\.venv\Scripts\python.exe -m ops.capsule_tool sign .\runtime\tiny-decoder .\capsule-private.pem
```

Transfer the XML and BIN pairs and the signed `manifest.json` to the server through an operator channel. Import them into a state directory that the server can access.

```powershell
.\.venv\Scripts\python.exe -m ops.capsule_tool import .\runtime\tiny-decoder .\runtime
```

In the administration console, open **Capsules** and select **Approve**. Approved projects can then submit jobs for this release through `/v2`. Withdrawing a release stops it from accepting new requests and marks open jobs as failed. Back up the imported files in the server state volume together with the public key and database.

## Run the decoder

The following request passes token IDs from the tokenizer that matches the model. The miniature decoder is synthetic and untrained. Its tokens are for technical validation.

```json
{"capsule":"tiny-decoder","version":1,"tokens":[1,4],"max_new_tokens":3}
```

Send this JSON to `POST /v2/jobs` with the project API key in `Authorization: Bearer ...`. Poll `GET /v2/jobs/{job_id}` and read the `tokens` array. `GET /v2/capsules` lists approved releases and their contracts. For a `tensor` capsule, send a flattened `values` array and retrieve the binary output with `GET /v2/jobs/{job_id}/result`. Decode the little-endian response with the shape and type listed in the catalogue. The submission JSON body is limited to 96 KiB, and v2 binary results can be up to 8 MiB.

With `distinct_nodes` set to its default of `true`, you need at least as many available nodes authorized for the project as there are stages. The server assigns one stage per node for the job and keeps the assignment while that node remains available. To test on a single computer, send `"distinct_nodes":false` explicitly. Each stage has a 300-second lease. The scheduler can reassign the stage after expiry. Nodes must trust the correct capsule key and remain available.

The first decoder stage receives a static context of i32 tokens with shape `[1, length]` and right padding. The last stage returns logits for all positions in the context with shape `[1, length, vocabulary]`. The graph must include an appropriate causal mask. At each step, the server picks the highest-scoring token at the last occupied position, appends it to the prefix, and starts again at the first stage. This implementation recomputes the prefix without a KV cache. The release supports up to 8 stages, tensors up to 8 MiB, assets up to 512 MiB per file, and up to 64 new tokens within the context limit. Pretrained models require a compatible export and validation on the target hardware.

## Distribute the signed Windows package

Use an Authenticode code-signing certificate in `Cert:\CurrentUser\My` on the packaging workstation. In the packaging command above, set `$signerThumbprint` to the thumbprint shown by Windows and `$timestampUrl` to the HTTPS URL of the timestamp service. The packager signs and timestamps the installer and executable, then signs a SHA-256 manifest covering every package file. The capsule key is optional for a package that only handles v1 jobs.

Share the certificate SHA-256 fingerprint shown by the packager and, for v2, the SHA-256 fingerprint of the 32 raw bytes of the Ed25519 public key through a channel separate from the package. Before running the installer, run the verifier from a separate trusted source, such as an approved checkout. In the example, `$package` is the package directory, `$publisherFingerprint` is the publisher certificate fingerprint, and `$capsuleKeyFingerprint` is the capsule key fingerprint.

```powershell
C:\sources\open-npu\verify-agent-package.ps1 -Package $package -PublisherSha256 $publisherFingerprint
& (Join-Path $package 'install-agent.ps1') -PublisherSha256 $publisherFingerprint -CapsuleKeySha256 $capsuleKeyFingerprint
```

Use `-CapsuleKeySha256` for packages containing `capsule-signing.pub`. Rotating the capsule key on an enrolled computer requires an explicit update to its trusted key.

## Verify and cancel a job

Add `"verify_results": true` to `POST /v2/jobs` to run each stage on two distinct node identities. The server always checks tensor sizes and rejects non-finite floating-point values. When verification is enabled, it also requires exact integer matches, compares floating-point results with absolute and relative tolerances of 0.001, and requires an exact match for the token selected by the decoder. The job fails if the outputs disagree. Verification increases compute costs and requires two compatible nodes. The check uses enrolled agent identities and provides no hardware attestation.

Cancel a queued or running job through `POST /v2/jobs/{job_id}/cancel` or the **Capsules** page in the administration console.
