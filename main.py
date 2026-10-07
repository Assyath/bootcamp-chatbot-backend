import os
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import get_db
from database.models import Conversation, Message
import json  
from fastapi.responses import StreamingResponse  # streaming des réponses

load_dotenv()

RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
RODIUMAI_API_KEY = os.environ["RODIUMAI_API_KEY"]
# Liste des modèles officiellement autorisés par le serveur
ALLOWED_MODELS = {
    "openai/gpt-4o-mini": "GPT-4o Mini (Par défaut)",
    "anthropic/claude-haiku-4-5-20251001": "Claude 4.5 Haiku"
}

# Le modèle par défaut si rien n'est sélectionné
DEFAULT_MODEL = "openai/gpt-4o-mini"

PREVIEW_LENGTH = 60

# Roles the LLM understands. Anything else stored in the DB (e.g. notifications) stays out of its prompt.
LLM_ROLES = {"user", "assistant"}
NOTIFICATION_ROLE = "system-notification"
NOTE_ROLE = "note"
NOTIFICATION_EVERY = 10  # a notification each time the dialogue reaches a multiple of this many messages
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."

def charger_prompt_systeme() -> str:
    """Charge le prompt système spécialisé depuis son fichier markdown."""
    try:
        path = os.path.join("prompts", "system.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        # Sécurité de secours si le fichier est introuvable
        return (
            "Tu es Study Buddy, un tuteur bienveillant spécialisé en Python. "
            "Réponds de manière pédagogique sans donner directement les réponses."
        )


app = FastAPI(title="Study Buddy Chatbot")


class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None  # first user message, truncated; None while the conversation is empty


class ChatRequest(BaseModel):
    conversation_id: int
    message: str
    model: str | None = None  # Reçoit le modèle sélectionné par le front



class ChatResponse(BaseModel):
    reply: str
    notification: str | None = None  # set when this turn also stored a system-notification


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime

class NoteRequest(BaseModel):
    content: str    


def load_messages(db: Session, conversation_id: int) -> list[Message]:
    # A conversation's messages in order; 404 if the conversation doesn't exist.
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    ).all()


def build_llm_history(rows: list[Message]) -> list[dict]:
    # The DB history is not necessarily what the LLM sees: filter it here, just before building the request.
    # For now we only drop the roles the LLM doesn't know (system-notification).
    return [{"role": m.role, "content": m.content} for m in rows if m.role in LLM_ROLES]


@app.get("/models")
def list_models():
    """Renvoie la liste des modèles autorisés pour l'interface utilisateur."""
    return [{"id": key, "name": value} for key, value in ALLOWED_MODELS.items()]


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    # Newest first, each joined to its first message (seq 1, always the user's) for the preview.
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(Message, and_(Message.conversation_id == Conversation.id, Message.seq == 1))
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [
        MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
        for m in load_messages(db, conversation_id)
    ]


@app.post("/conversations/{conversation_id}/notes", status_code=201)
def add_note(conversation_id: int, req: NoteRequest, db: Session = Depends(get_db)):
    # Vérifier si la conversation existe
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    
    # Récupérer les messages existants pour calculer le prochain numéro de séquence (seq)
    rows = load_messages(db, conversation_id)
    next_seq = rows[-1].seq + 1 if rows else 1
    
    # Enregistrer la note en base de données
    nouvelle_note = Message(
        conversation_id=conversation_id,
        seq=next_seq,
        role=NOTE_ROLE,
        content=req.content
    )
    db.add(nouvelle_note)
    db.commit()
    
    return {"status": "Note enregistrée avec succès", "seq": next_seq}


@app.post("/chat")
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    # 1. Déterminer le modèle à utiliser (celui du client ou celui par défaut)
    chosen_model = req.model if req.model else DEFAULT_MODEL
    
    # 2. Sécurité : Refuser si le modèle n'est pas autorisé
    if chosen_model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="Modèle non autorisé par le serveur.")

    # 3. Charger l'historique depuis la base de données
    rows = load_messages(db, req.conversation_id)
    history = build_llm_history(rows)
    
    # Calcul de la séquence de départ pour les nouveaux messages
    next_seq = rows[-1].seq + 1 if rows else 1
    user_message = {"role": "user", "content": req.message}

    # Préparation finale de la liste envoyée au LLM (avec le prompt système)
    messages = [{"role": "system", "content": charger_prompt_systeme()}, *history, user_message]

    # 4. Définition du Générateur (la fonction interne qui gère le streaming)
    def event_generator():
        reply_accumulated = ""
        
               # Configuration du bonus : on s'autorise jusqu'à 3 tentatives automatiques
        tentatives_max = 3
        connexion_reussie = False

        for tentative in range(1, tentatives_max + 1):
            try:
                # Appel à l'API externe avec client httpx en mode streaming
                with httpx.stream(
                    "POST",
                    RODIUMAI_URL,
                    headers={"Authorization": f"Bearer {RODIUMAI_API_KEY}"},
                    json={
                        "model": chosen_model,
                        "messages": messages,
                        "max_tokens": 512,
                        "stream": True,
                    },
                    timeout=30,
                ) as response:
                    response.raise_for_status()
                    connexion_reussie = True # On valide que l'API a répondu
                    
                    # On lit les lignes renvoyées par l'API au fil de l'eau
                    for line in response.iter_lines():
                        if not line:
                            continue
                        
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            
                            try:
                                data_json = json.loads(data_str)
                                chunk = data_json["choices"]["delta"].get("content", "")
                                if chunk:
                                    reply_accumulated += chunk
                                    yield f"data: {json.dumps({'text': chunk})}\n\n"
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
                    
                    # Si on est arrivé ici sans accroc, on sort de la boucle des tentatives
                    break
                                
            except httpx.HTTPError as exc:
                # Si on a encore des tentatives en réserve, on écrit un log et on réessaye
                if tentative < tentatives_max:
                    print(f"[Bonus Re-soumission] Échec tentative {tentative}/{tentatives_max}. Nouvelle tentative...")
                    continue
                else:
                    # Si toutes les tentatives ont échoué, on transmet l'erreur visuellement
                    yield f"data: {json.dumps({'error': 'Erreur de connexion persistante avec le LLM après 3 essais.'})}\n\n"
                    return

        # 5. SAUVEGARDE EN BASE : Exécutée uniquement si tout le flux s'est terminé avec succès !
        if reply_accumulated:
            db.add_all([
                Message(conversation_id=req.conversation_id, seq=next_seq, role="user", content=req.message),
                Message(conversation_id=req.conversation_id, seq=next_seq + 1, role="assistant", content=reply_accumulated),
            ])

            # Gestion automatique de l'ancienne logique de notification système toutes les 10 lignes
            if (len(history) + 2) % NOTIFICATION_EVERY == 0:
                db.add(Message(
                    conversation_id=req.conversation_id,
                    seq=next_seq + 2,
                    role=NOTIFICATION_ROLE,
                    content=NOTIFICATION_TEXT
                ))

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                yield f"data: {json.dumps({'error': 'Erreur de concurrence lors de la sauvegarde.'})}\n\n"
                return
        
        # Signal final envoyé au client pour dire que tout est terminé et sauvegardé proprement
        yield "data: [DONE]\n\n"

    # 6. On retourne la StreamingResponse configurée avec le bon type MIME du streaming (text/event-stream)
    return StreamingResponse(event_generator(), media_type="text/event-stream")
