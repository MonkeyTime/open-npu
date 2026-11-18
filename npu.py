"""Sélection des NPU Windows et chargement des graphes ONNX autonomes."""

from contextlib import contextmanager
from pathlib import Path
import sys

import numpy as np

AMD_PROVIDER = "VitisAIExecutionProvider"


def intel_device(core):
    devices = core.available_devices
    target = next((device for device in devices if device.partition(".")[0] == "NPU"), None)
    if target is None:
        raise RuntimeError(f"NPU Intel indisponible dans OpenVINO. Périphériques vus : {devices}")
    return target


def onnx_bytes(path):
    from onnx import TensorProto, load_model_from_string
    from google.protobuf.descriptor import FieldDescriptor
    from google.protobuf.message import DecodeError

    data = path.read_bytes()
    try:
        pending = [load_model_from_string(data)]
    except DecodeError as error:
        raise ValueError("Graphe ONNX invalide.") from error
    while pending:
        message = pending.pop()
        if isinstance(message, TensorProto):
            if message.external_data or message.data_location == TensorProto.EXTERNAL:
                raise ValueError("Tous les poids ONNX doivent être inclus dans le fichier signé.")
            continue
        for field, value in message.ListFields():
            if field.type == FieldDescriptor.TYPE_MESSAGE:
                pending.extend(value if field.is_repeated else (value,))
    return data


@contextmanager
def amd_npu():
    if sys.getwindowsversion().build < 26100:
        raise RuntimeError("Le NPU AMD via Windows ML requiert Windows 11 24H2 ou ultérieur.")

    from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
    from winui3.microsoft.windows.applicationmodel.dynamicdependency.bootstrap import initialize

    init_apartment(ApartmentType.MULTI_THREADED)
    try:
        try:
            runtime = initialize()
        except OSError as error:
            raise RuntimeError("Windows App Runtime 2.3 ou ultérieur est requis pour le NPU AMD.") from error
        with runtime:
            path, error = amd_provider_path()
            if not path:
                raise RuntimeError(error)
            import onnxruntime as ort

            ort.register_execution_provider_library(AMD_PROVIDER, path)
            try:
                device = next((device for device in ort.get_ep_devices()
                               if device.ep_name == AMD_PROVIDER
                               and device.device.type == ort.OrtHardwareDeviceType.NPU), None)
                if device is None:
                    raise RuntimeError("Le fournisseur Vitis AI ne signale aucun périphérique NPU.")
                yield ort, device
            finally:
                ort.unregister_execution_provider_library(AMD_PROVIDER)
    finally:
        uninit_apartment()


def amd_provider_path():
    from winui3.microsoft.windows.ai import machinelearning as winml

    catalog = winml.ExecutionProviderCatalog.get_default()
    for provider in catalog.find_all_providers():
        if provider.name != AMD_PROVIDER:
            continue
        if not provider.library_path:
            result = provider.ensure_ready_async().get()
            if result.status != winml.ExecutionProviderReadyResultState.SUCCESS or not provider.library_path:
                return None, f"Fournisseur NPU AMD indisponible : {result.diagnostic_text}"
        return provider.library_path, None
    return None, "Windows ML ne détecte aucun fournisseur de NPU AMD Ryzen AI compatible."


def amd_session(accelerator, model):
    ort, device = accelerator
    options = ort.SessionOptions()
    options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
    options.add_provider_for_devices([device], {})
    session = ort.InferenceSession(model, sess_options=options, enable_fallback=False, read_config_from_model=False)
    if AMD_PROVIDER not in session.get_providers():
        raise RuntimeError("La session ONNX n’a pas activé le fournisseur NPU AMD.")
    return session


def probe_amd():
    # Une convolution 1×1 multiplie chaque canal par deux. Les valeurs
    # choisies sont représentables exactement après conversion en BF16.
    values = (np.arange(1024, dtype=np.float32) % 15 - 7).reshape(1, 16, 8, 8)
    model = Path(__file__).with_name("npu-probe.onnx")
    with amd_npu() as accelerator:
        session = amd_session(accelerator, model.read_bytes())
        result, = session.run(None, {session.get_inputs()[0].name: values})
        del session
    if not np.array_equal(result, values * 2):
        raise RuntimeError("L’autotest AMD Ryzen AI a produit des résultats inattendus.")
