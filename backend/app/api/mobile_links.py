from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.settings import Settings, get_settings

router = APIRouter(tags=["mobile links"])

LINK_PATHS = [
    "/invite/*",
    "/verify*",
    "/reset-password*",
    "/verify-email-change*",
    "/applications/*",
    "/g/*",
    "/home/*",
]


@router.get("/.well-known/assetlinks.json")
async def android_asset_links(
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    fingerprints = [
        value.strip().upper()
        for value in settings.mobile_android_sha256_cert_fingerprints.split(",")
        if value.strip()
    ]
    if not fingerprints:
        raise HTTPException(status_code=404, detail={"code": "MOBILE_LINKS_NOT_CONFIGURED"})
    return [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": settings.mobile_android_package,
                "sha256_cert_fingerprints": fingerprints,
            },
        }
    ]


@router.get("/.well-known/apple-app-site-association")
async def apple_app_site_association(
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    app_ids = [value.strip() for value in settings.mobile_ios_app_ids.split(",") if value.strip()]
    if not app_ids:
        raise HTTPException(status_code=404, detail={"code": "MOBILE_LINKS_NOT_CONFIGURED"})
    return {
        "applinks": {
            "apps": [],
            "details": [
                {
                    "appIDs": app_ids,
                    "components": [{"/": path} for path in LINK_PATHS],
                }
            ],
        }
    }
