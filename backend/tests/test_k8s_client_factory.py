"""Tests for K8sClientFactory's client-construction path: credential-failure
containment (nothing decrypted may ever reach an exception message or a log
line) and the token-auth CA-cert temp file's lifecycle.

These don't touch a real cluster -- `client.Configuration`/`ApiClient`
construction is pure in-memory object building, no network calls happen
until an actual API call is made.
"""

import json
import logging
import os
from datetime import UTC, datetime

from app.models.cluster import Cluster
from app.security import encrypt_str
from app.services.k8s import K8sClientFactory, K8sUnavailableError


def _cluster(**overrides) -> Cluster:
    defaults: dict = {
        "id": 1,
        "name": "test-cluster",
        "display_name": "test-cluster",
        "k8s_auth_kind": "kubeconfig",
        "prometheus_url": "http://localhost:30090",
        "alertmanager_url": "http://localhost:30093",
        "rules_namespace": "kam-rules",
        "webhook_token_hash": "x",
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Cluster(**defaults)


def _token_cluster(*, ca_cert: str | None = None, **overrides) -> Cluster:
    creds: dict = {"token": "abc123"}
    if ca_cert is not None:
        creds["ca_cert"] = ca_cert
    defaults = {
        "k8s_auth_kind": "token",
        "k8s_api_url": "https://cluster.invalid:6443",
        "credentials_encrypted": encrypt_str(json.dumps(creds)),
    }
    defaults.update(overrides)
    return _cluster(**defaults)


# -- credential-failure containment --------------------------------------


def test_corrupt_credentials_raise_unavailable_without_leaking_the_raw_error(
    caplog,
) -> None:
    # A garbage, non-Fernet string simulates a corrupted/tampered
    # credentials_encrypted column -- decrypt_str raises before we even get
    # to YAML.
    secret_fragment = "not-a-real-fernet-token-zzz"
    cluster = _cluster(credentials_encrypted=secret_fragment)
    factory = K8sClientFactory()

    with caplog.at_level(logging.WARNING):
        try:
            factory.get(cluster)
            raised = None
        except K8sUnavailableError as exc:
            raised = exc

    assert raised is not None
    assert "credentials/config invalid" in str(raised)
    assert secret_fragment not in str(raised)

    assert any("test-cluster" in record.message for record in caplog.records)
    for record in caplog.records:
        assert secret_fragment not in record.message
        assert secret_fragment not in record.getMessage()


def test_malformed_yaml_after_decrypt_is_contained(caplog) -> None:
    # Valid Fernet ciphertext, but it decrypts to something that isn't valid
    # YAML -- simulates a corrupted-but-not-tampered credentials column.
    bad_yaml = "{unterminated: [brackets, no closing"
    cluster = _cluster(credentials_encrypted=encrypt_str(bad_yaml))
    factory = K8sClientFactory()

    with caplog.at_level(logging.WARNING):
        try:
            factory.get(cluster)
            raised = None
        except K8sUnavailableError as exc:
            raised = exc

    assert raised is not None
    assert "credentials/config invalid" in str(raised)
    assert bad_yaml not in str(raised)
    for record in caplog.records:
        assert bad_yaml not in record.message


def test_valid_kubeconfig_yaml_missing_required_fields_is_contained(caplog) -> None:
    # Valid YAML, but not a usable kubeconfig -- the kubernetes client's own
    # config loader should reject this, and that failure must be contained
    # the same way.
    incomplete_kubeconfig = "apiVersion: v1\nkind: Config\n"
    cluster = _cluster(credentials_encrypted=encrypt_str(incomplete_kubeconfig))
    factory = K8sClientFactory()

    with caplog.at_level(logging.WARNING):
        try:
            factory.get(cluster)
            raised = None
        except K8sUnavailableError as exc:
            raised = exc

    assert raised is not None
    assert "credentials/config invalid" in str(raised)


def test_deliberate_unavailable_error_passes_through_unwrapped() -> None:
    # "token auth kind but no credentials stored" is our own safe, already-
    # typed message -- it must reach the caller as-is, not get re-wrapped
    # into the generic "credentials/config invalid" message.
    cluster = _cluster(k8s_auth_kind="token", credentials_encrypted=None)
    factory = K8sClientFactory()

    try:
        factory.get(cluster)
        raised = None
    except K8sUnavailableError as exc:
        raised = exc

    assert raised is not None
    assert "no credentials stored" in str(raised)


# -- token-auth CA cert temp file lifecycle -------------------------------

CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfakefakefakefake\n-----END CERTIFICATE-----\n"


def test_ca_cert_is_written_to_a_fresh_0600_temp_file() -> None:
    cluster = _token_cluster(ca_cert=CA_PEM)
    factory = K8sClientFactory()

    api_client = factory.get(cluster)

    ca_path = factory._cache[cluster.id][2]
    try:
        assert ca_path is not None
        assert os.path.exists(ca_path)
        mode = os.stat(ca_path).st_mode & 0o777
        assert mode == 0o600
        with open(ca_path) as f:
            assert f.read() == CA_PEM
        assert api_client.configuration.ssl_ca_cert == ca_path
    finally:
        os.unlink(ca_path)


def test_ca_cert_path_is_not_the_old_predictable_scheme() -> None:
    # The old implementation wrote to a fixed, guessable
    # f"kam-cluster-{cluster.id}-ca.pem" path -- any other process on the
    # box could predict and read/replace it ahead of time. mkstemp's
    # filename must not follow that pattern.
    cluster = _token_cluster(ca_cert=CA_PEM)
    factory = K8sClientFactory()

    factory.get(cluster)
    ca_path = factory._cache[cluster.id][2]
    try:
        assert os.path.basename(ca_path) != f"kam-cluster-{cluster.id}-ca.pem"
    finally:
        os.unlink(ca_path)


def test_ca_cert_file_is_cleaned_up_when_cache_entry_is_evicted() -> None:
    cluster = _token_cluster(ca_cert=CA_PEM)
    factory = K8sClientFactory()

    factory.get(cluster)
    first_path = factory._cache[cluster.id][2]
    assert os.path.exists(first_path)

    # Simulate the cluster row being edited (e.g. credentials rotated).
    cluster.updated_at = datetime(2026, 2, 1, tzinfo=UTC)
    factory.get(cluster)
    second_path = factory._cache[cluster.id][2]

    try:
        assert second_path != first_path
        assert not os.path.exists(first_path)
        assert os.path.exists(second_path)
    finally:
        os.unlink(second_path)


def test_no_ca_cert_disables_ssl_verification_and_writes_no_temp_file() -> None:
    cluster = _token_cluster(ca_cert=None)
    factory = K8sClientFactory()

    api_client = factory.get(cluster)

    assert factory._cache[cluster.id][2] is None
    assert api_client.configuration.verify_ssl is False
