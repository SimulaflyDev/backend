"""Regression coverage for catalog products being replaced by invented furniture."""

import base64
import io
import uuid
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.routers import visualization
from app.services import azure_ai_client
from app.services.image_service import persist_image


def _png(colour, size=(80, 40)):
    output = io.BytesIO()
    Image.new("RGB", size, colour).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def image_client(monkeypatch):
    configuration = SimpleNamespace(
        ai_configured=True,
        AZURE_IMAGE_EDIT_DEPLOYMENT="test-image-edit",
        AZURE_IMAGE_GEN_DEPLOYMENT="test-image-gen",
        AZURE_AI_FOUNDRY_ENDPOINT="https://images.example.invalid",
        AZURE_AI_FOUNDRY_API_KEY="test-key",
    )
    monkeypatch.setattr(azure_ai_client, "get_settings", lambda: configuration)
    return azure_ai_client.AzureImageClient()


@pytest.mark.parametrize("colours", [["brown"], ["brown", "beige", "green"]])
async def test_edit_wire_request_contains_room_and_all_product_photos(
    monkeypatch, image_client, colours
):
    """Inspect the actual multipart body, not just arguments to a stub SDK."""
    requests = []
    rendered = _png("white")

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(rendered).decode()}],
        })

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        azure_ai_client.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    room = _png("blue", size=(160, 90))
    references = [_png(colour) for colour in colours]
    if len(references) == 1:
        result = await image_client.image_edit(room, references[0], "Keep this product", size="auto")
    else:
        result = await image_client.image_edit(
            room, None, "Keep these products", product_images=references, size="auto",
        )

    assert result == rendered
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path.endswith("/test-image-edit/images/edits")
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
        + request.content
    )
    parts = list(message.iter_parts())
    fields = {
        part.get_param("name", header="content-disposition"): part.get_content()
        for part in parts if part.get_filename() is None
    }
    assert fields["input_fidelity"] == "high"
    assert fields["n"] == "1"
    assert fields["size"] == "auto"

    files = [part for part in parts if part.get_filename() is not None]
    assert [part.get_param("name", header="content-disposition") for part in files] == [
        "image[]"
    ] * (len(references) + 1)
    assert [part.get_filename() for part in files] == [
        "room.png", *[f"product_{i}.png" for i in range(1, len(references) + 1)],
    ]
    for part, source in zip(files, [room, *references], strict=True):
        with Image.open(io.BytesIO(part.get_payload(decode=True))) as sent:
            with Image.open(io.BytesIO(source)) as original:
                # No square crop, lost components, or changed product colours.
                assert sent.size == original.size
                assert sent.convert("RGB").tobytes() == original.convert("RGB").tobytes()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
async def test_product_preview_never_falls_back_to_text_generation(image_client, status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://test.invalid/edits"))
    error = httpx.HTTPStatusError("edit unavailable", request=response.request, response=response)
    image_client._post_multipart = AsyncMock(side_effect=error)
    image_client.image_gen = AsyncMock()

    with pytest.raises(httpx.HTTPStatusError):
        await image_client.image_edit(_png("blue"), _png("brown"), "Place sofa")

    image_client.image_gen.assert_not_awaited()


@pytest.mark.parametrize("disabled_field", ["ai_configured", "AZURE_IMAGE_EDIT_DEPLOYMENT"])
async def test_product_preview_requires_edit_deployment(image_client, disabled_field):
    setattr(image_client.settings, disabled_field, False if disabled_field == "ai_configured" else "")
    image_client.image_gen = AsyncMock()
    with pytest.raises(RuntimeError, match="configured image-edit deployment"):
        await image_client.image_edit(_png("blue"), _png("brown"), "Place sofa")
    image_client.image_gen.assert_not_awaited()


@pytest.mark.parametrize("bad_image", [b"", b"<html>Image unavailable</html>"])
async def test_invalid_reference_cannot_produce_a_successful_preview(image_client, bad_image):
    image_client._post_multipart = AsyncMock()
    image_client.image_gen = AsyncMock()
    with pytest.raises(ValueError, match="product 1 image"):
        await image_client.image_edit(_png("blue"), bad_image, "Place sofa")
    image_client._post_multipart.assert_not_awaited()
    image_client.image_gen.assert_not_awaited()


async def test_general_image_generation_keeps_its_existing_fallback(image_client):
    image_client.settings.AZURE_IMAGE_EDIT_DEPLOYMENT = ""
    image_client.image_gen = AsyncMock(return_value=b"general-image")
    result = await image_client.image_edit(_png("blue"), None, "Restyle", fallback_prompt="Style scene")
    assert result == b"general-image"
    image_client.image_gen.assert_awaited_once_with("Style scene", size="1024x1024")


async def test_relative_merchant_photo_loads_from_image_store(
    monkeypatch, db_session, test_user
):
    photo = _png("brown")
    image = await persist_image(
        db_session, owner_id=test_user.id, data=photo,
        media_type="image/png", source="merchant_product_images",
    )
    await db_session.commit()
    monkeypatch.setattr(
        visualization, "SessionLocal",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    assert await visualization._fetch_product_image(f"/api/v1/upload/room-image/{image.id}") == photo
    with pytest.raises(ValueError, match="Could not load"):
        await visualization._fetch_product_image(f"/api/v1/upload/room-image/{uuid.uuid4()}")


async def test_external_product_image_follows_cdn_redirect(monkeypatch):
    photo = _png("brown")
    requests = []

    async def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/primary.png"})
        return httpx.Response(200, content=photo, headers={"content-type": "image/png"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        visualization.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    assert await visualization._fetch_product_image("https://catalog.invalid/redirect") == photo
    assert requests == ["/redirect", "/primary.png"]


@pytest.fixture
def worker_dependencies(monkeypatch):
    client = SimpleNamespace(image_edit=AsyncMock(return_value=b"rendered-image"))
    persist = AsyncMock()
    failed = AsyncMock()
    monkeypatch.setattr(visualization, "get_image_client", lambda: client)
    monkeypatch.setattr(visualization, "_persist_and_mark_done", persist)
    monkeypatch.setattr(visualization, "mark_failed", failed)
    return client, persist, failed


def _product(title, category):
    return SimpleNamespace(
        id=uuid.uuid4(), title=title, category=category,
        image_url=f"https://catalog.invalid/{category}.png", product_metadata={},
    )


def _job_args():
    return dict(
        job_id=uuid.uuid4(), user_id=uuid.uuid4(), session_id=uuid.uuid4(),
        room_bytes=_png("blue"), room_summary="A room", placement="Left wall",
    )


async def test_single_worker_uses_the_selected_product_primary_photo(monkeypatch, worker_dependencies):
    client, persist, failed = worker_dependencies
    sofa = _product("Sofa 8 Seater", "sofa")
    photo = _png("brown")
    fetch = AsyncMock(return_value=photo)
    monkeypatch.setattr(visualization, "_fetch_product_image", fetch)
    await visualization._run_single(product=sofa, **_job_args())

    fetch.assert_awaited_once_with(sofa.image_url)
    args = client.image_edit.await_args
    assert args.args[1] == photo
    assert 'Image 2 is the exact product to place: "Sofa 8 Seater"' in args.args[2]
    assert "fallback_prompt" not in args.kwargs
    assert persist.await_args.kwargs["product_id"] == sofa.id
    failed.assert_not_awaited()


async def test_composite_worker_keeps_each_photo_mapped_to_its_product(monkeypatch, worker_dependencies):
    client, persist, failed = worker_dependencies
    products = [_product("Brown sofa", "sofa"), _product("Beige table", "table")]
    photos = {products[0].image_url: _png("brown"), products[1].image_url: _png("beige")}
    monkeypatch.setattr(visualization, "_fetch_product_image", AsyncMock(side_effect=photos.__getitem__))
    await visualization._run_composite(products=products, **_job_args())

    args = client.image_edit.await_args
    assert args.kwargs["product_images"] == list(photos.values())
    assert 'Image 2: "Brown sofa"' in args.args[2]
    assert 'Image 3: "Beige table"' in args.args[2]
    assert "fallback_prompt" not in args.kwargs
    persist.assert_awaited_once()
    failed.assert_not_awaited()


@pytest.mark.parametrize("composite", [False, True])
async def test_missing_photo_fails_job_without_publishing_substitute(
    monkeypatch, worker_dependencies, composite
):
    client, persist, failed = worker_dependencies
    monkeypatch.setattr(
        visualization, "_fetch_product_image",
        AsyncMock(side_effect=ValueError("Could not load selected product photo")),
    )
    product = _product("Sofa 8 Seater", "sofa")
    if composite:
        await visualization._run_composite(products=[product], **_job_args())
    else:
        await visualization._run_single(product=product, **_job_args())

    client.image_edit.assert_not_awaited()
    persist.assert_not_awaited()
    failed.assert_awaited_once()
