"""Upload an IPA and apply Kaede's standard encryption / no France declaration."""

import base64
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


API = "https://api.appstoreconnect.apple.com/v1/"
# These are the release owner's App Store Connect questionnaire answers.
# Revisit before distributing in France or changing the cryptography used.
ANSWERS = {
    "containsProprietaryCryptography": False,
    "containsThirdPartyCryptography": True,
    "availableOnFrenchStore": False,
}


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def token(key_file):
    now = int(time.time())
    header = {"alg": "ES256", "kid": os.environ["APP_STORE_CONNECT_KEY_ID"], "typ": "JWT"}
    claims = {"iss": os.environ["APP_STORE_CONNECT_ISSUER_ID"], "iat": now,
              "exp": now + 600, "aud": "appstoreconnect-v1"}
    message = b".".join(b64(json.dumps(part).encode()) for part in (header, claims))
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_file],
        input=message, capture_output=True, check=True,
    ).stdout
    # OpenSSL returns DER; ES256 JWTs require two unsigned 32-byte integers.
    if signature[:1] != b"\x30" or signature[1] != len(signature) - 2:
        raise ValueError("Expected a P-256 DER signature")
    values = []
    offset = 2
    for _ in range(2):
        if signature[offset] != 2:
            raise ValueError("Expected an ECDSA signature integer")
        size = signature[offset + 1]
        value = int.from_bytes(signature[offset + 2:offset + 2 + size], "big")
        values.append(value.to_bytes(32, "big"))
        offset += 2 + size
    if offset != len(signature):
        raise ValueError("Unexpected ECDSA signature data")
    return (message + b"." + b64(b"".join(values))).decode()


def request(key_file, method, path, data=None):
    url = path if path.startswith(API) else API + path
    if not url.startswith(API):
        raise ValueError("Unexpected App Store Connect URL")
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps({"data": data}).encode() if data is not None else None,
        headers={"Authorization": "Bearer " + token(key_file),
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"App Store Connect {method} returned {error.code}: "
            + error.read().decode(errors="replace")
        ) from None


def ipa_info(path):
    with zipfile.ZipFile(path) as ipa:
        names = [name for name in ipa.namelist()
                 if name.startswith("Payload/") and name.endswith(".app/Info.plist")
                 and name.count("/") == 2]
        if len(names) != 1:
            raise ValueError("Expected exactly one main app in the IPA")
        return plistlib.loads(ipa.read(names[0]))


def deliver(ipa, key_file):
    info = ipa_info(ipa)

    def api(method, path, data=None):
        return request(key_file, method, path, data)

    apps = api("GET", "apps?" + urllib.parse.urlencode({
        "filter[bundleId]": info["CFBundleIdentifier"],
    }))["data"]
    if len(apps) != 1:
        raise RuntimeError("Could not identify a unique App Store Connect app")
    app_id = apps[0]["id"]
    build_path = "builds?" + urllib.parse.urlencode({
        "filter[app]": app_id,
        "filter[version]": info["CFBundleVersion"],
        "filter[preReleaseVersion.version]": info["CFBundleShortVersionString"],
        "filter[preReleaseVersion.platform]": "IOS",
    })

    def find_build():
        builds = api("GET", build_path)["data"]
        if len(builds) > 1:
            raise RuntimeError("More than one build matches the IPA")
        return builds[0] if builds else None

    build = find_build()
    if build is None:
        subprocess.run([
            "xcrun", "altool", "--upload-app", "--type", "ios", "--file", ipa,
            "--apiKey", os.environ["APP_STORE_CONNECT_KEY_ID"],
            "--apiIssuer", os.environ["APP_STORE_CONNECT_ISSUER_ID"],
        ], check=True)
    else:
        print("Build already uploaded; resuming encryption declaration.", flush=True)

    deadline = time.monotonic() + 1800
    while True:
        build = find_build()
        state = build["attributes"]["processingState"] if build else "not yet visible"
        if state == "VALID":
            break
        if state in ("FAILED", "INVALID"):
            raise RuntimeError(f"Apple build processing failed: {state}")
        if time.monotonic() >= deadline:
            raise RuntimeError("Apple processing timed out; rerun the failed TestFlight job")
        print(f"Waiting for Apple build processing: {state}", flush=True)
        time.sleep(30)

    declaration = None
    path = "appEncryptionDeclarations?" + urllib.parse.urlencode({"filter[app]": app_id})
    while path and declaration is None:
        page = api("GET", path)
        for candidate in page["data"]:
            attrs = candidate["attributes"]
            if (attrs.get("appEncryptionDeclarationState") == "APPROVED"
                    and attrs.get("platform") in (None, "IOS")
                    and all(attrs.get(key) is value for key, value in ANSWERS.items())):
                declaration = candidate
                break
        path = page.get("links", {}).get("next")
    if declaration is None:
        declaration = api("POST", "appEncryptionDeclarations", {
            "type": "appEncryptionDeclarations",
            "attributes": {**ANSWERS, "appDescription":
                           "Kaede Chat is a messaging app with end-to-end encryption."},
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
        })["data"]

    api("PATCH", "builds/" + build["id"], {
        "type": "builds", "id": build["id"],
        "relationships": {"appEncryptionDeclaration": {"data": {
            "type": "appEncryptionDeclarations", "id": declaration["id"],
        }}},
    })
    linked = api("GET", "builds/" + build["id"] + "/appEncryptionDeclaration")["data"]
    if linked["id"] != declaration["id"]:
        raise RuntimeError("Apple did not retain the encryption declaration")
    print("TestFlight encryption declaration applied: standard encryption; no France.", flush=True)


if __name__ == "__main__":
    deliver(*sys.argv[1:])
