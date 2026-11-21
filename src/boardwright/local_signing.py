# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Signer binding for signed local releases (audit F-R7, ``docs/LOCAL_FIRST.md`` section 5.1b).

A cryptographically valid signature is not enough: the release commit and
tag of a signed local release must be signed by the approver recorded in
the frozen approval. Three facts are kept apart:

- ``signature_valid``: the signature verifies cryptographically
  (``ssh-keygen -Y check-novalidate``), whoever made it;
- ``signer_matches_approver``: OpenSSH verifies the signature for the
  approver's e-mail as principal (``ssh-keygen -Y verify -I <email>``, with
  ``gpg.ssh.revocationFile`` when set) **and** the signing key appears in
  ``gpg.ssh.allowedSignersFile`` for exactly that one literal principal;
- ``authenticated``: always false. The mapping from key to approver is the
  developer's local configuration, not an independent authority.

Only explicit interfaces are used: exit statuses of ``ssh-keygen -Y``, the
documented SSHSIG blob (OpenSSH ``PROTOCOL.sshsig``) for the signing key, and
the documented allowed-signers file format. Human-readable verification
output is never parsed.

Supported: SSH signing (``gpg.format ssh``). OpenPGP and X.509 signing are
refused for ``--sign`` (identity binding is not implemented for them) and an
existing signature in those formats is reported as unverifiable, never as
verified. Everything that cannot be decided fails closed: a missing signature
or file, a key listed for another or for several principals, wildcard
principals, ``cert-authority`` or other options on the key's line.
"""

from __future__ import annotations

import base64
import os
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .local_release import LocalReleaseError

SSH_BEGIN = b"-----BEGIN SSH SIGNATURE-----"
SSH_END = b"-----END SSH SIGNATURE-----"
OTHER_ARMOUR = {b"-----BEGIN PGP SIGNATURE-----": "openpgp", b"-----BEGIN SIGNED MESSAGE-----": "x509"}
NAMESPACE = "git"
ALLOWED_OPTIONS = ('namespaces="git"',)
_EMAIL = re.compile(r"<([^<>\s]+@[^<>\s]+)>\s*$")
_WILDCARD = re.compile(r"[*?!]")


class SigningError(LocalReleaseError):
    """A signed local release cannot be made or verified with the signer bound to the approver."""


@dataclass
class SignatureReport:
    """What a signed (or unsigned) local release proves about its signatures; ``authenticated`` is never true."""

    signed: bool
    approver: str = ""
    signature_valid: bool | None = None
    signer_matches_approver: bool | None = None
    authenticated: bool = False
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"signed": self.signed, "approver": self.approver, "signature_valid": self.signature_valid,
                "signer_matches_approver": self.signer_matches_approver, "authenticated": False}

    def line(self) -> str:
        if not self.signed:
            return "signature: unsigned (local approval, unauthenticated)"
        yes = {True: "yes", False: "NO", None: "unknown"}
        return (f"signature: valid {yes[self.signature_valid]}; signer matches approver {self.approver or '?'} "
                f"{yes[self.signer_matches_approver]}; authenticated no (local allowed-signers mapping, "
                "not an independent authority)")


# -- configuration -----------------------------------------------------------

def _config(repo: Path, key: str) -> str:
    done = subprocess.run(["git", "config", key], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8")  # git config is UTF-8 whatever the locale
    return done.stdout.strip() if done.returncode == 0 else ""


def signing_format(repo: Path) -> str:
    return (_config(repo, "gpg.format") or "openpgp").lower()


def _program(repo: Path) -> str:
    return _config(repo, "gpg.ssh.program") or "ssh-keygen"


def approver_email(approval: dict) -> str:
    """The e-mail of the frozen approval's approver (``Name <email>``); fails closed when absent."""

    match = _EMAIL.search(str(approval.get("approver") or ""))
    if not match:
        raise SigningError("The approval record names no approver e-mail; a signed release cannot be bound to it.")
    return match.group(1)


def _allowed_signers(repo: Path) -> Path:
    configured = _config(repo, "gpg.ssh.allowedSignersFile")
    if not configured:
        raise SigningError("gpg.ssh.allowedSignersFile is not set: the signer cannot be bound to the approver.")
    path = Path(os.path.expanduser(configured))
    if not path.is_absolute():
        path = (repo / path).resolve()
    if not path.is_file():
        raise SigningError(f"The allowed-signers file {path} does not exist.")
    return path


def _revocation(repo: Path) -> list[str]:
    configured = _config(repo, "gpg.ssh.revocationFile")
    return ["-r", os.path.expanduser(configured)] if configured else []


# -- key material ----------------------------------------------------------

def _read_string(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(data):
        raise ValueError("truncated")
    (length,) = struct.unpack(">I", data[offset:offset + 4])
    end = offset + 4 + length
    if end > len(data):
        raise ValueError("truncated")
    return data[offset + 4:end], end


def signature_key(armoured: bytes) -> bytes:
    """The signer's public key blob from an armoured SSHSIG (``PROTOCOL.sshsig``)."""

    body = armoured.split(SSH_BEGIN, 1)[1].split(SSH_END, 1)[0]
    blob = base64.b64decode(b"".join(body.split()), validate=True)
    if blob[:6] != b"SSHSIG":
        raise ValueError("not an SSHSIG blob")
    key, _ = _read_string(blob, 10)  # magic (6) + version (4)
    return key


def _key_type(blob: bytes) -> str:
    kind, _ = _read_string(blob, 0)
    return kind.decode("ascii", "replace")


def signing_key_blob(repo: Path) -> bytes:
    """The configured signing key (``user.signingkey``) as a public key blob."""

    configured = _config(repo, "user.signingkey")
    if not configured:
        raise SigningError("Signing was requested but no signing key is configured (user.signingkey).")
    if configured.startswith("key::"):
        text = configured[5:]
    else:
        path = Path(os.path.expanduser(configured))
        public = path if path.suffix == ".pub" else Path(str(path) + ".pub")
        if public.is_file():
            text = public.read_text(encoding="utf-8")
        elif path.is_file():
            done = subprocess.run([_program(repo), "-y", "-f", str(path)], capture_output=True, text=True,
                                  encoding="utf-8")
            if done.returncode != 0:
                raise SigningError(f"The public key of the signing key {path} cannot be read.")
            text = done.stdout
        else:
            raise SigningError(f"The signing key {configured} does not exist.")
    parts = text.split()
    for index, token in enumerate(parts[:-1]):
        try:
            blob = base64.b64decode(parts[index + 1], validate=True)
            if _key_type(blob) == token:
                return blob
        except (ValueError, struct.error):
            continue
    raise SigningError("The signing key is not an SSH public key.")


def principals_for(allowed: Path, key: bytes) -> list[str]:
    """Every principal listed for ``key`` in an allowed-signers file; fails closed on anything not literal.

    Raises for wildcard principals, ``cert-authority`` or any option other
    than ``namespaces="git"`` on a line for this key, so a key that could
    stand for more than one identity is never treated as bound to one.
    """

    found: list[str] = []
    for number, raw in enumerate(allowed.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        for index in range(1, len(tokens) - 1):
            try:
                blob = base64.b64decode(tokens[index + 1], validate=True)
            except ValueError:
                continue
            try:
                if _key_type(blob) != tokens[index]:
                    continue
            except (ValueError, struct.error):
                continue
            if blob != key:
                break
            options = tokens[1:index]
            if any(option not in ALLOWED_OPTIONS for option in options):
                raise SigningError(f"{allowed} line {number} lists the signing key with options "
                                   f"{' '.join(options)}; only literal principals are supported.")
            principals = tokens[0].split(",")
            if any(_WILDCARD.search(p) for p in principals):
                raise SigningError(f"{allowed} line {number} lists the signing key for a principal pattern; "
                                   "only literal principals are supported.")
            found.extend(principals)
            break
    return found


def _bound_to(principals: list[str], email: str) -> bool:
    return sorted(set(principals)) == [email]


def preflight(repo: Path, approval: dict) -> str:
    """Before building or tagging with ``--sign``: the configured key must stand for the approver alone."""

    if signing_format(repo) != "ssh":
        raise SigningError(f"--sign with gpg.format {signing_format(repo)} is not supported: the signer can be "
                           "bound to the approver for SSH signing only (gpg.format ssh). Nothing was done.")
    email = approver_email(approval)
    principals = principals_for(_allowed_signers(repo), signing_key_blob(repo))
    if not _bound_to(principals, email):
        raise SigningError(f"The signing key is listed for {', '.join(principals) or 'no principal'} in the "
                           f"allowed-signers file, not for the approver {email} alone. Nothing was done.")
    return email


# -- verification ------------------------------------------------------------

def _split(repo: Path, obj: str, kind: str) -> tuple[bytes, bytes]:
    """``(payload, signature)`` of a commit or tag object; the signature is empty when there is none."""

    raw = subprocess.run(["git", "cat-file", kind, obj], cwd=repo, capture_output=True, check=True).stdout
    if kind == "tag":
        for marker in (SSH_BEGIN, *OTHER_ARMOUR):
            index = raw.find(marker)
            if index >= 0:
                return raw[:index], raw[index:]
        return raw, b""
    header, separator, message = raw.partition(b"\n\n")
    kept, signature, in_signature = [], [], False
    for line in header.split(b"\n"):
        if line.startswith((b"gpgsig ", b"gpgsig-sha256 ")):
            in_signature = True
            signature.append(line.split(b" ", 1)[1])
        elif in_signature and line.startswith(b" "):
            signature.append(line[1:])
        else:
            in_signature = False
            kept.append(line)
    return b"\n".join(kept) + separator + message, (b"\n".join(signature) + b"\n") if signature else b""


def verify_object(repo: Path, obj: str, kind: str, email: str) -> tuple[bool | None, bool, str]:
    """``(signature_valid, signer_matches_approver, problem)`` for one commit or tag."""

    payload, signature = _split(repo, obj, kind)
    label = f"the release {'commit' if kind == 'commit' else 'tag'} {obj[:12] if kind == 'commit' else obj}"
    if not signature:
        return False, False, f"{label} is not signed although the release is recorded as signed."
    if not signature.startswith(SSH_BEGIN):
        kind_name = next((name for marker, name in OTHER_ARMOUR.items() if signature.startswith(marker)), "unknown")
        return None, False, (f"{label} has a {kind_name} signature; its signer cannot be bound to the approver "
                             "(SSH signing only). It is not treated as verified.")
    program = _program(repo)
    with tempfile.TemporaryDirectory(prefix="bw-sig-") as temp:
        sig = Path(temp) / "signature"
        sig.write_bytes(signature)
        valid = subprocess.run([program, "-Y", "check-novalidate", "-n", NAMESPACE, "-s", str(sig)],
                               input=payload, capture_output=True).returncode == 0
        if not valid:
            return False, False, f"{label}: the signature is not cryptographically valid."
        try:
            allowed = _allowed_signers(repo)
            principals = principals_for(allowed, signature_key(signature))
        except (SigningError, ValueError, struct.error) as exc:
            return True, False, f"{label}: the signer cannot be bound to the approver: {exc}"
        verified = subprocess.run([program, "-Y", "verify", "-f", str(allowed), "-I", email, "-n", NAMESPACE,
                                   "-s", str(sig), *_revocation(repo)], input=payload,
                                  capture_output=True).returncode == 0
    if not verified or not _bound_to(principals, email):
        listed = ", ".join(principals) or "no principal"
        return True, False, (f"{label} is signed by a key listed for {listed}, not by the approver {email} alone "
                             "(a valid signature by another signer is refused).")
    return True, True, ""


def release_signatures(repo: Path, version: str, record: dict, approval: dict) -> SignatureReport:
    """The signature facts of a finalised local release (``record`` is ``finalize.json``)."""

    if not record.get("signed"):
        return SignatureReport(signed=False)
    report = SignatureReport(signed=True)
    try:
        report.approver = approver_email(approval)
    except SigningError as exc:
        report.signature_valid, report.signer_matches_approver = None, False
        report.problems.append(str(exc))
        return report
    objects = [(record["release_commit"], "commit")]
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{version}"], cwd=repo,
                      capture_output=True).returncode == 0:
        objects.append((version, "tag"))
    valid_all, match_all = True, True
    for obj, kind in objects:
        valid, matches, problem = verify_object(repo, obj, kind, report.approver)
        valid_all = valid_all and bool(valid)
        match_all = match_all and matches
        if problem:
            report.problems.append(problem)
    report.signature_valid, report.signer_matches_approver = valid_all, match_all
    return report
