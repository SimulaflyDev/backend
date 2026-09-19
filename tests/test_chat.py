import base64
import uuid

import pytest
from langchain_core.messages import AIMessage

from app.models.merchant import Merchant
from app.models.merchant_product import MerchantProduct
from app.models.product import Product
from app.services import rag_service
from app.services.rag_service import RAGResult, _strip_directives


@pytest.fixture(autouse=True)
def _stub_chat_llm(monkeypatch):
    """Provide a deterministic chat LLM for the vision /analyze path.

    Returns an AIMessage object (what AzureChatOpenAI.ainvoke returns) so the
    router's `response.content` access works exactly like in production.
    """

    class _StubChatLLM:
        async def ainvoke(self, messages):
            return AIMessage(
                content="This is a minimalist living room with plenty of natural light. "
                "Are you looking to add seating or some decor?"
            )

    from app.services import llm as llm_module

    monkeypatch.setattr(llm_module, "get_chat_llm", lambda **kw: _StubChatLLM())
    # chat.py imports the symbol directly, so patch it there too.
    from app.routers import chat as chat_router

    monkeypatch.setattr(chat_router, "get_chat_llm", lambda **kw: _StubChatLLM())


@pytest.fixture
def stub_run_rag_turn(monkeypatch, db_session):
    """Stubs the whole LangGraph turn so chat tests focus on router + DB wiring."""

    async def _fake(
        db,
        *,
        session_id,
        user_message,
        context_summary,
        design_profile,
        image_data_url=None,
    ):
        # echo a product carousel when the user mentions 'sofa', otherwise plain text
        products: list = []
        if "sofa" in user_message.lower():
            from app.models.merchant_product import MerchantProduct
            res = await db.execute(
                __import__("sqlalchemy").select(MerchantProduct).where(MerchantProduct.category == "Sofa").limit(2)
            )
            products = list(res.scalars().all())
        return RAGResult(
            products=products,
            assistant_text=f"Great, here are some options for: {user_message[:40]}",
            preview_product_id=None,
            preview_product_ids=None,
            shopping_intent=bool(products),
            image_generation_prompt=(
                user_message if "generate an image" in user_message.lower() else None
            ),
            suggested_questions=[
                "Tell me more",
                "Show me an example",
                "What should I ask next?",
            ],
        )

    from app.routers import chat as chat_router

    monkeypatch.setattr(chat_router, "run_rag_turn", _fake)
    return _fake


@pytest.mark.asyncio
async def test_chat_analyze_persists_context_and_image(auth_client, db_session):
    r = await auth_client.post("/api/v1/sessions/", json={"title": "Analyze test"})
    sid = r.json()["id"]

    img = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9").decode()
    r = await auth_client.post(
        "/api/v1/chat/analyze",
        json={"session_id": sid, "image_base64": img, "media_type": "image/jpeg"},
    )
    assert r.status_code == 200, r.text
    assert "minimalist" in r.json()["content"]


@pytest.mark.asyncio
async def test_chat_turn_returns_carousel_when_sofa_in_catalog(
    auth_client, db_session, stub_run_rag_turn
):
    merchant = Merchant(
        slug=f"ch-{uuid.uuid4().hex[:6]}",
        legal_name="Chat Test Merchant",
        display_name="CTM",
        referral_code=f"CTM-{uuid.uuid4().hex[:6].upper()}",
    )
    db_session.add(merchant)
    await db_session.commit()
    await db_session.refresh(merchant)

    product = MerchantProduct(
        merchant_id=merchant.id,
        sku="C1",
        title="Mid-Century Sofa",
        category="Sofa",
        in_app_price=14999,
        status="published",
        primary_image_url="https://images.example/sofa-front.jpg",
        additional_images=[
            "https://images.example/sofa-side.jpg",
            "https://images.example/sofa-back.jpg",
        ],
    )
    db_session.add(product)
    await db_session.commit()

    r = await auth_client.post("/api/v1/sessions/", json={"title": "Chat test"})
    sid = r.json()["id"]

    r = await auth_client.post(
        "/api/v1/chat/",
        json={"session_id": sid, "content": "I need a comfy sofa under 15000 rupees"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"].startswith("Great, here are some options")
    assert body["ui_payload"]["type"] == "product_carousel"
    assert any(p["sku"] == "C1" for p in body["ui_payload"]["products"])
    card = next(p for p in body["ui_payload"]["products"] if p["sku"] == "C1")
    assert card["primary_image_url"] == product.primary_image_url
    assert card["additional_images"] == product.additional_images
    # Persisted chat messages must retain the same gallery for later sessions.
    history = await auth_client.get(f"/api/v1/chat/{sid}/messages")
    assert history.status_code == 200
    saved_card = history.json()[-1]["ui_payload"]["products"][0]
    assert saved_card["additional_images"] == product.additional_images


@pytest.mark.asyncio
async def test_chat_turn_without_catalog_match_returns_text_only(
    auth_client, db_session, stub_run_rag_turn
):
    r = await auth_client.post("/api/v1/sessions/", json={"title": "Chat text"})
    sid = r.json()["id"]

    r = await auth_client.post(
        "/api/v1/chat/",
        json={"session_id": sid, "content": "What do you think of minimalism?"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ui_payload"]["type"] == "suggestions"
    assert len(body["ui_payload"]["suggestions"]) == 3
    assert "minimalism" in body["content"].lower() or body["content"].startswith("Great")


@pytest.mark.asyncio
async def test_chat_image_request_starts_job_in_same_session(
    auth_client, stub_run_rag_turn, monkeypatch
):
    from app.routers import chat as chat_router

    async def _no_op_worker(**kwargs):
        return None

    monkeypatch.setattr(chat_router, "_run_prompt_image_generation", _no_op_worker)

    created = await auth_client.post(
        "/api/v1/sessions/", json={"title": "Image chat"}
    )
    sid = created.json()["id"]
    response = await auth_client.post(
        "/api/v1/chat/",
        json={"session_id": sid, "content": "Generate an image of a moonlit garden"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["image_task_id"] is not None

    messages = await auth_client.get(f"/api/v1/chat/{sid}/messages")
    assert [message["role"] for message in messages.json()] == ["user", "assistant"]


def test_strip_directives_extracts_preview():
    text = (
        "Here is a sofa.\n"
        "PRODUCTS_JSON: [\"abc\"]\n"
        f"PREVIEW_REQUEST: {{\"product_id\": \"{uuid.uuid4()}\"}}"
    )
    cleaned, preview, preview_multi = _strip_directives(text)
    assert "PRODUCTS_JSON" not in cleaned
    assert "PREVIEW_REQUEST" not in cleaned
    assert preview is not None


def test_extracts_suggested_questions():
    text, suggestions = rag_service._extract_suggestions(
        'Answer text.\nSUGGESTIONS_JSON: ["First?", "Second?", "Third?"]'
    )
    assert text == "Answer text."
    assert suggestions == ["First?", "Second?", "Third?"]


def test_image_intent_fallback_is_conservative():
    assert rag_service._heuristic_image_intent(
        "Generate an image of a moonlit garden"
    )
    assert not rag_service._heuristic_image_intent(
        "What does this image show?"
    )


def test_graph_builds():
    # Graph compiles without running LLMs — pure structural check.
    graph = rag_service._build_graph()
    assert graph is not None
