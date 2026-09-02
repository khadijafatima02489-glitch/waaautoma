"""Controlled AI ordering: the model can phrase and classify, while tools own all business mutations."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from emergentintegrations.llm.chat import LlmChat, UserMessage

from database import NO_ID, db
from services import order_service

logger = logging.getLogger(__name__)
FALLBACK = "Sorry, I'm having a little trouble right now. Please try again or ask our team for help."
TOOLS = [{"type": "function", "function": {"name": "add_to_cart", "description": "Add a menu item by name.",
          "parameters": {"type": "object", "properties": {"item_name": {"type": "string"}, "quantity": {"type": "integer"}}, "required": ["item_name"]}}},
         {"type": "function", "function": {"name": "remove_from_cart", "description": "Remove an item by name.", "parameters": {"type": "object", "properties": {"item_name": {"type": "string"}}, "required": ["item_name"]}}},
         {"type": "function", "function": {"name": "calculate_cart", "description": "Calculate the current cart.", "parameters": {"type": "object", "properties": {}}}},
         {"type": "function", "function": {"name": "set_order_type", "description": "Set delivery or pickup.", "parameters": {"type": "object", "properties": {"order_type": {"type": "string", "enum": ["delivery", "pickup"]}}, "required": ["order_type"]}}},
         {"type": "function", "function": {"name": "set_customer_details", "description": "Save customer name and delivery address.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "address": {"type": "string"}}}}},
         {"type": "function", "function": {"name": "create_order", "description": "Place an explicitly confirmed order.", "parameters": {"type": "object", "properties": {}}}},
         {"type": "function", "function": {"name": "get_order_status", "description": "Look up an order number.", "parameters": {"type": "object", "properties": {"order_number": {"type": "integer"}}, "required": ["order_number"]}}},
         {"type": "function", "function": {"name": "request_human_support", "description": "Hand this chat to staff.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}}}}]


def _menu_text(categories, items):
    groups = {}
    for item in items:
        groups.setdefault(item["category_id"], []).append(item)
    return "\n".join([f"### {category['name']}\n" + "\n".join(f"- {i['name']} — {i['price']:.0f} {i.get('description', '')}" for i in groups.get(category["id"], [])) for category in categories]) or "(No menu configured)"


def _system_prompt(restaurant, settings, conversation, customer, categories, items, recent):
    history = "\n".join(f"{'Customer' if m['direction'] == 'in' else 'You'}: {m['text']}" for m in recent[-8:])
    return f"""You are the short, warm WhatsApp ordering assistant for {restaurant['name']}.
Reply in the customer's language: English, Urdu script, or Roman Urdu.
Only use the configured menu and call tools for every cart, detail, total, and order action. Never invent prices.
Use WhatsApp formatting only: bold text uses one asterisk on each side (*bold*), never Markdown double asterisks (**bold**).
When the customer explicitly confirms a complete summary, call create_order immediately.
For delivery collect a name and address; for pickup collect a name.
MENU:\n{_menu_text(categories, items)}
CART: {conversation.get('cart', [])}\nORDER TYPE: {conversation.get('order_type') or 'not set'}\nCUSTOMER: {conversation.get('customer_name') or customer.get('name') or 'unknown'}
RECENT:\n{history or '(first message)'}"""


async def _dispatch(name, args, restaurant, items, conversation_id, customer):
    conversation = await db.conversations.find_one({"id": conversation_id}, NO_ID)
    cart = conversation.get("cart", [])
    totals = lambda: order_service.compute_totals(restaurant, cart, conversation.get("order_type"))
    if name == "add_to_cart":
        item = order_service.match_menu_item(items, args.get("item_name", ""))
        if not item or not item.get("available", True):
            return {"error": "item_not_found", "available_items": [i["name"] for i in items if i.get("available", True)]}
        quantity = max(1, int(args.get("quantity", 1) or 1))
        existing = next((c for c in cart if c["item_id"] == item["id"]), None)
        if existing:
            existing["qty"] += quantity
        else:
            cart.append({"item_id": item["id"], "name": item["name"], "unit_price": float(item["price"]), "qty": quantity})
        await db.conversations.update_one({"id": conversation_id}, {"$set": {"cart": cart, "state": "SELECTING_ITEMS"}})
        return {"ok": True, "cart": cart, "totals": totals()}
    if name == "remove_from_cart":
        item = order_service.match_menu_item(items, args.get("item_name", ""))
        cart = [c for c in cart if not item or c["item_id"] != item["id"]]
        await db.conversations.update_one({"id": conversation_id}, {"$set": {"cart": cart}})
        return {"ok": True, "cart": cart, "totals": totals()}
    if name == "calculate_cart":
        return {"cart": cart, "totals": totals()}
    if name == "set_order_type":
        order_type = args.get("order_type")
        conversation["order_type"] = order_type
        await db.conversations.update_one({"id": conversation_id}, {"$set": {"order_type": order_type}})
        return {"ok": True, "order_type": order_type, "totals": totals()}
    if name == "set_customer_details":
        updates = {k: args[k] for k in ("name", "address") if args.get(k)}
        if updates:
            if "name" in updates:
                conversation["customer_name"] = updates["name"]
                await db.customers.update_one({"id": customer["id"]}, {"$set": {"name": updates["name"]}})
            await db.conversations.update_one({"id": conversation_id}, {"$set": {"customer_name": conversation.get("customer_name", ""), **updates}})
        return {"ok": True, **updates}
    if name == "create_order":
        if not cart or not conversation.get("order_type") or (conversation["order_type"] == "delivery" and not conversation.get("address")):
            return {"error": "missing_order_details"}
        if not (conversation.get("customer_name") or customer.get("name")):
            return {"error": "name_missing"}
        if totals()["subtotal"] < float(restaurant.get("min_order", 0)):
            return {"error": "below_minimum", "minimum": restaurant.get("min_order")}
        order = await order_service.create_order(restaurant=restaurant, conversation=conversation, customer=customer)
        await db.conversations.update_one({"id": conversation_id}, {"$set": {"cart": [], "state": "ORDER_PLACED", "last_order_number": order["order_number"], "last_order_id": order["id"]}})
        return {"_order_created": True, "order": order, "order_number": order["order_number"], "total": order["total"]}
    if name == "get_order_status":
        order = await db.orders.find_one({"restaurant_id": restaurant["id"], "order_number": int(args.get("order_number", 0))}, NO_ID)
        return {"order_number": order["order_number"], "status": order["status"], "total": order["total"]} if order else {"error": "order_not_found"}
    if name == "request_human_support":
        await db.conversations.update_one({"id": conversation_id}, {"$set": {"ai_active": False, "state": "HUMAN_HANDOFF"}})
        return {"ok": True, "handoff": True}
    return {"error": "unknown_tool"}


async def generate_reply(*, restaurant, ai_settings, conversation, customer, categories, items, recent_messages, incoming_text):
    try:
        chat = (LlmChat(api_key=os.environ["EMERGENT_LLM_KEY"], session_id=conversation["id"],
                        system_message=_system_prompt(restaurant, ai_settings, conversation, customer, categories, items, recent_messages))
                .with_model("gemini", ai_settings.get("model") or os.environ.get("AI_MODEL", "gemini-3-flash-preview"))
                .with_tools(TOOLS, tool_choice="auto"))
        response = await chat.send_message_with_tools(UserMessage(text=incoming_text))
        created_order = None
        for _ in range(6):
            if not getattr(response, "tool_calls", None):
                break
            for tool_call in response.tool_calls:
                try:
                    args = tool_call.arguments if isinstance(tool_call.arguments, dict) else json.loads(tool_call.arguments or "{}")
                except Exception:
                    args = {}
                result = await _dispatch(tool_call.name, args, restaurant, items, conversation["id"], customer)
                if result.get("_order_created"):
                    created_order = result.pop("order")
                    result.pop("_order_created", None)
                chat.add_tool_result(tool_call.id, json.dumps(result, default=str))
            response = await chat.send_message_with_tools()
        return ((response.content or "").strip() or "Ji, main aap ki kya madad kar sakta hoon?"), created_order
    except Exception as exc:
        logger.exception("AI generate_reply failed: %s", exc)
        return FALLBACK, None