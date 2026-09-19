"""Panel uploads must reach every consumer product representation unchanged."""

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_count", [0, 1, 4])
async def test_panel_gallery_round_trips_to_app(
    auth_client, verify_merchant, monkeypatch, extra_count
):
    from app.services import embedding

    async def no_embedding(*args, **kwargs):
        pass

    monkeypatch.setattr(embedding, "regenerate_embedding", no_embedding)
    response = await auth_client.post("/api/v1/merchants/", json={
        "legal_name": "Gallery Shop", "display_name": "Gallery Shop",
        "settings": {"onboarding_completed": True},
    })
    assert response.status_code == 201, response.text
    merchant = response.json()
    await verify_merchant(merchant["id"])
    headers = {"X-Merchant-Id": merchant["id"]}
    primary = "https://images.example/front.jpg"
    extras = [f"https://images.example/angle-{i}.jpg" for i in range(extra_count)]
    response = await auth_client.post("/api/v1/merchant/products/", headers=headers, json={
        "sku": "GALLERY-1", "title": "Gallery chair", "category": "Chair",
        "status": "published", "has_simulafly_listing": True,
        "in_app_price": 1200, "in_app_stock": 8,
        "primary_image_url": primary, "additional_images": extras,
    })
    assert response.status_code == 201, response.text
    product_id = response.json()["id"]
    assert response.json()["additional_images"] == extras

    for path in ("/api/v1/saved/", "/api/v1/cart/"):
        response = await auth_client.post(path, json={"product_id": product_id})
        assert response.status_code == 201, response.text

    async def assert_consumer_photos(expected):
        response = await auth_client.get(f"/api/v1/products/{product_id}")
        assert response.status_code == 200, response.text
        representations = [response.json()]
        for path in (
            "/api/v1/products/",
            f"/api/v1/merchants/public/{merchant['slug']}/products",
        ):
            response = await auth_client.get(path)
            assert response.status_code == 200, response.text
            representations.append(next(p for p in response.json() if p["id"] == product_id))
        for path in ("/api/v1/saved/", "/api/v1/cart/"):
            response = await auth_client.get(path)
            assert response.status_code == 200, response.text
            representations.append(response.json()["items"][0]["product"])
        for product in representations:
            assert product["primary_image_url"] == primary
            assert product["additional_images"] == expected

    await assert_consumer_photos(extras)
    # Reordered/replaced panel photos must appear on the very next app fetch.
    updated = ["https://images.example/new-back.jpg", "https://images.example/new-side.jpg"]
    response = await auth_client.patch(
        f"/api/v1/merchant/products/{product_id}", headers=headers,
        json={"additional_images": updated},
    )
    assert response.status_code == 200, response.text
    await assert_consumer_photos(updated)
    response = await auth_client.patch(
        f"/api/v1/merchant/products/{product_id}", headers=headers,
        json={"additional_images": []},
    )
    assert response.status_code == 200, response.text
    await assert_consumer_photos([])
