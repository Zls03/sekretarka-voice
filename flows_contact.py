# flows_contact.py - System kontaktu z właścicielem
"""
Obsługuje:
- Prośby o kontakt z właścicielem
- Przekierowanie rozmowy (transfer)
- Zbieranie i wysyłanie wiadomości

Działa dla OBU trybów:
- Rezerwacje włączone
- Tylko informacyjnie
"""

from pipecat_flows import FlowManager, FlowsFunctionSchema
from loguru import logger
import asyncio

# Lazy imports - unikamy circular import
# from flows import end_conversation_function, create_end_node, send_message_email


# ============================================================================
# GŁÓWNA FUNKCJA - GPT wywołuje gdy klient chce kontakt
# ============================================================================

def contact_owner_function(tenant: dict) -> FlowsFunctionSchema:
    """Klient chce kontakt z właścicielem (wiadomość LUB połączenie)"""
    return FlowsFunctionSchema(
        name="contact_owner",
        description="""Klient chce kontakt z właścicielem. Użyj gdy:
- "chcę porozmawiać z właścicielem"
- "proszę o kontakt"  
- "czy mogę zostawić wiadomość"
- "połącz mnie"
- "przekieruj mnie"
- "chcę rozmawiać z człowiekiem"
- klient jest sfrustrowany i potrzebuje pomocy człowieka
- nie możesz pomóc i klient potrzebuje właściciela
- klient chce się umówić ale rezerwacje są wyłączone (zaproponuj wiadomość)""",
        properties={
            "reason": {
                "type": "string",
                "description": "Krótko: dlaczego klient chce kontakt"
            },
            "customer_name": {
                "type": "string",
                "description": "Imię klienta jeśli już podał"
            },
            "message": {
                "type": "string",
                "description": "Treść wiadomości jeśli klient już powiedział co przekazać"
            }
        },
        required=["reason"],
        handler=lambda args, fm: handle_contact_owner(args, fm, tenant),
    )


async def handle_contact_owner(args: dict, flow_manager: FlowManager, tenant: dict):
    """KOD decyduje: dać wybór (wiadomość/połączenie) czy tylko wiadomość"""
    reason = args.get("reason", "")
    customer_name = args.get("customer_name", "")
    message = args.get("message", "")
    
    logger.info(f"📞 Contact owner: reason='{reason}', name='{customer_name}', msg='{message[:30] if message else ''}'")
    
    # Zapisz dane jeśli już podane
    if customer_name:
        flow_manager.state["contact_name"] = customer_name
    if message:
        flow_manager.state["contact_message"] = message
    
    # Sprawdź czy transfer dostępny
    transfer_enabled = tenant.get("transfer_enabled", 0) == 1
    transfer_number = tenant.get("transfer_number", "")
    has_transfer = transfer_enabled and transfer_number
    
    logger.info(f"📞 Transfer available: {has_transfer} (enabled={transfer_enabled}, number='{transfer_number}')")
    
    # Jeśli mamy już imię i wiadomość - zapisz od razu
    if customer_name and message:
        logger.info("📞 Have name and message - saving directly")
        return await save_and_confirm_message(flow_manager, tenant, customer_name, message)
    
    # Jeśli transfer dostępny - daj wybór
    if has_transfer:
        return (None, create_contact_choice_node(tenant))
    else:
        # Tylko wiadomość
        if customer_name:
            return (None, create_collect_message_content_node(tenant))
        else:
            return (None, create_collect_contact_name_node(tenant))


# ============================================================================
# NODE: Wybór - wiadomość czy połączenie (gdy transfer dostępny)
# ============================================================================

def create_contact_choice_node(tenant: dict) -> dict:
    """KOD pyta - wiadomość czy połączenie? ENUM wymusza poprawny wybór"""
    return {
        "name": "contact_choice",
        "pre_actions": [
            {"type": "tts_say", "text": "Mogę przekazać wiadomość do właściciela lub połączyć bezpośrednio. Co Pan woli?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": """Klient wybiera: wiadomość lub połączenie z właścicielem.
You must ALWAYS use contact_choice function to progress - NIGDY nie odpowiadaj tekstem!"""
        }],
        "task_messages": [{
            "role": "system",
            "content": """You must ALWAYS use contact_choice to progress the conversation.

Klient odpowiada: wiadomość czy połączenie.

WYWOŁAJ contact_choice:
- "wiadomość", "zostawić", "niech oddzwoni", "nie trzeba łączyć" → choice="wiadomość"
- "połączenie", "połączyć", "połącz", "bezpośrednio", "teraz", "tak połącz", "tak", "proszę" → choice="połączenie"

⚠️ KRYTYCZNE:
- Jeśli klient mówi "tak" lub "bezpośrednio" lub "połączyć" → choice="połączenie"
- NIGDY nie mów "nie mogę połączyć" - ZAWSZE wywołaj contact_choice!
- ZAKAZ odpowiadania tekstem bez wywołania funkcji!"""
        }],
        "functions": [
            contact_choice_function(tenant),
        ]
    }

def contact_choice_function(tenant: dict) -> FlowsFunctionSchema:
    """ENUM + fallback - GPT nie może się pomylić"""
    return FlowsFunctionSchema(
        name="contact_choice",
        description="""Klient wybrał sposób kontaktu. MUSISZ wywołać tę funkcję!
- Jeśli klient chce POŁĄCZENIE/BEZPOŚREDNIO/TAK/PROSZĘ → choice="połączenie"
- Jeśli klient chce WIADOMOŚĆ/ZOSTAWIĆ/ODDZWONIĆ → choice="wiadomość"
- W KAŻDYM INNYM przypadku → choice="wiadomość" (domyślnie)""",
        properties={
            "choice": {
                "type": "string",
                "enum": ["wiadomość", "połączenie"],
                "description": "Wybór klienta: 'połączenie' jeśli chce rozmawiać teraz, 'wiadomość' w każdym innym przypadku"
            }
        },
        required=["choice"],
        handler=lambda args, fm: handle_contact_choice(args, fm, tenant),
    )


async def handle_contact_choice(args: dict, flow_manager: FlowManager, tenant: dict):
    """KOD wykonuje wybór"""
    choice = args.get("choice", "")
    
    logger.info(f"📞 Contact choice: {choice}")
    
    if choice == "połączenie":
        return await execute_transfer(flow_manager, tenant)
    else:
        # Wiadomość
        if flow_manager.state.get("contact_name"):
            return (None, create_collect_message_content_node(tenant))
        else:
            return (None, create_collect_contact_name_node(tenant))


# ============================================================================
# NODE: Zbieranie imienia
# ============================================================================

def create_collect_contact_name_node(tenant: dict) -> dict:
    """Zbierz imię klienta"""
    return {
        "name": "collect_contact_name",
        "pre_actions": [
            {"type": "tts_say", "text": "Dobrze, przekażę wiadomość. Jak się Pan nazywa?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": "Zbierasz imię klienta do wiadomości dla właściciela."
        }],
        "task_messages": [{
            "role": "system",
            "content": """Klient poda imię/nazwisko.

Gdy powie → wywołaj set_contact_name
Jeśli rezygnuje ("nie", "nieważne") → end_conversation"""
        }],
        "functions": [
            set_contact_name_function(tenant),
            _get_end_conversation_function(),
        ]
    }


def set_contact_name_function(tenant: dict) -> FlowsFunctionSchema:
    """Zapisz imię"""
    return FlowsFunctionSchema(
        name="set_contact_name",
        description="Klient podał imię/nazwisko",
        properties={
            "name": {"type": "string", "description": "Imię lub nazwisko klienta"}
        },
        required=["name"],
        handler=lambda args, fm: handle_set_contact_name(args, fm, tenant),
    )


async def handle_set_contact_name(args: dict, flow_manager: FlowManager, tenant: dict):
    """Zapisz imię i przejdź do treści"""
    name = args.get("name", "").strip()
    
    # Walidacja
    invalid_names = ["pan", "pani", "tak", "nie", "halo", "słucham", "proszę"]
    if not name or len(name) < 2 or name.lower() in invalid_names:
        return ({"status": "error", "message": "Nie dosłyszałam. Jak mogę zapisać?"}, 
                create_collect_contact_name_node(tenant))  # ← RETRY!
    
    # Usuń "pan/pani" z początku
    for prefix in ["pan ", "pani "]:
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    
    name = name.strip().title()
    flow_manager.state["contact_name"] = name
    logger.info(f"📝 Contact name: {name}")
    
    # Jeśli już mamy wiadomość - zapisz
    if flow_manager.state.get("contact_message"):
        return await save_and_confirm_message(
            flow_manager, tenant, name, flow_manager.state["contact_message"]
        )
    
    return (None, create_collect_message_content_node(tenant))


# ============================================================================
# NODE: Zbieranie treści wiadomości
# ============================================================================

def create_collect_message_content_node(tenant: dict) -> dict:
    """Zbierz treść wiadomości"""
    return {
        "name": "collect_message_content",
        "pre_actions": [
            {"type": "tts_say", "text": "Co mam przekazać właścicielowi?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": "Zbierasz treść wiadomości dla właściciela."
        }],
        "task_messages": [{
            "role": "system",
            "content": """Klient poda treść wiadomości.

Gdy powie treść → wywołaj set_contact_message
Jeśli rezygnuje → end_conversation"""
        }],
        "functions": [
            set_contact_message_function(tenant),
            _get_end_conversation_function(),
        ]
    }


def set_contact_message_function(tenant: dict) -> FlowsFunctionSchema:
    """Zapisz treść wiadomości"""
    return FlowsFunctionSchema(
        name="set_contact_message",
        description="Klient podał treść wiadomości",
        properties={
            "message": {"type": "string", "description": "Treść wiadomości"}
        },
        required=["message"],
        handler=lambda args, fm: handle_set_contact_message(args, fm, tenant),
    )


async def handle_set_contact_message(args: dict, flow_manager: FlowManager, tenant: dict):
    """Zapisz wiadomość i wyślij"""
    message = args.get("message", "").strip()
    
    if not message or len(message) < 3:
        return ({"status": "error", "message": "Nie dosłyszałam. Co mam przekazać?"}, 
                create_collect_message_content_node(tenant))  # ← RETRY!
    
    name = flow_manager.state.get("contact_name", "Nieznany")
    
    return await save_and_confirm_message(flow_manager, tenant, name, message)


# ============================================================================
# AKCJE: Zapis wiadomości i Transfer
# ============================================================================

async def save_and_confirm_message(flow_manager: FlowManager, tenant: dict, name: str, message: str):
    """Zapisz wiadomość, wyślij email, potwierdź klientowi"""
    # Lazy import
    from flows import send_message_email, create_end_node
    
    caller_phone = flow_manager.state.get("caller_phone", "nieznany")
    
    logger.info(f"📧 Saving message: {name} - {message[:50]}...")
    
    # Wyślij email
    owner_email = tenant.get("notification_email") or tenant.get("email")
    
    if owner_email:
        try:
            await send_message_email(tenant, name, message, caller_phone, owner_email)
            logger.info(f"📧 Email sent to: {owner_email}")
        except Exception as e:
            logger.error(f"📧 Email error: {e}")
    else:
        logger.warning("📧 No owner email configured!")
    
    # Wyczyść state
    flow_manager.state["contact_name"] = None
    flow_manager.state["contact_message"] = None
    
    return (f"Dziękuję {name}. Przekazałam wiadomość do właściciela, który oddzwoni.",
            create_end_node(message_saved=True))


async def execute_transfer(flow_manager: FlowManager, tenant: dict):
    """Wykonaj transfer rozmowy do właściciela"""
    # Lazy import
    from flows import create_end_node
    from helpers import db
    
    transfer_number = tenant.get("transfer_number", "")
    
    if not transfer_number:
        logger.error("📞 No transfer number configured!")
        return ("Przepraszam, przekierowanie chwilowo niedostępne. Czy mogę przekazać wiadomość?",
                create_collect_contact_name_node(tenant))
    
    call_sid = flow_manager.state.get("call_sid")
    
    if not call_sid:
        logger.error("📞 No call_sid for transfer!")
        return ("Przepraszam, wystąpił problem. Czy mogę przekazać wiadomość?",
                create_collect_contact_name_node(tenant))
    
    # Formatuj numer
    transfer_number = transfer_number.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    if transfer_number.startswith("0048"):
        transfer_number = transfer_number[4:]
    elif transfer_number.startswith("48") and len(transfer_number) == 11:
        transfer_number = transfer_number[2:]
    if not transfer_number.startswith("+"):
        transfer_number = f"+48{transfer_number}"
    
    if len(transfer_number) < 12:
        logger.error(f"📞 Invalid transfer number: {transfer_number}")
        return ("Przepraszam, numer przekierowania jest nieprawidłowy. Czy mogę przekazać wiadomość?",
                create_collect_contact_name_node(tenant))
    
    logger.info(f"📞 Executing transfer: {call_sid} → {transfer_number}")
    
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transfer_requests (
                call_sid TEXT PRIMARY KEY,
                transfer_number TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        
        await db.execute(
            """INSERT OR REPLACE INTO transfer_requests (call_sid, transfer_number, status, created_at)
               VALUES (?, ?, 'pending', datetime('now'))""",
            [call_sid, transfer_number]
        )
        logger.info(f"📞 Transfer request saved")
        
    except Exception as e:
        logger.error(f"📞 Failed to save transfer: {e}")
        return ("Przepraszam, wystąpił problem z przekierowaniem. Czy mogę przekazać wiadomość?",
                create_collect_contact_name_node(tenant))
    
    flow_manager.state["transfer_requested"] = True
    flow_manager.state["conversation_ended"] = True
    
    # Zamknij WebSocket po 3s (czas na TTS)
    async def close_for_transfer():
        await asyncio.sleep(3.0)
        try:
            from pipecat.frames.frames import EndFrame
            await flow_manager.task.queue_frame(EndFrame())
            logger.info("🔚 EndFrame sent for transfer")
        except Exception as e:
            logger.error(f"Error sending EndFrame: {e}")
    
    asyncio.create_task(close_for_transfer())
    
    return ("Łączę z właścicielem, proszę chwilę poczekać.", _create_transfer_end_node())


def _create_transfer_end_node() -> dict:
    """Node końcowy dla transferu"""
    return {
        "name": "transfer_end",
        "respond_immediately": False,
        "pre_actions": [
            {"type": "tts_say", "text": "Łączę z właścicielem, proszę chwilę poczekać."}
        ],
        "post_actions": [
            {"type": "end_conversation"}
        ],
        "role_messages": [],
        "task_messages": [],
        "functions": []
    }


# ============================================================================
# HELPER: Pobierz end_conversation z flows.py (lazy)
# ============================================================================

def _get_end_conversation_function():
    """Lazy import end_conversation_function"""
    from flows import end_conversation_function
    return end_conversation_function()


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "contact_owner_function",
    "contact_choice_function",
    "set_contact_name_function", 
    "set_contact_message_function",
    "create_contact_choice_node",
    "create_collect_contact_name_node",
    "create_collect_message_content_node",
]