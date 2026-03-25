import logging
import os
import httpx
import gspread
from fastapi import APIRouter, Request
from oauth2client.service_account import ServiceAccountCredentials
from pydantic import json
from config.config import SF_INSTANCE_URL
from api.fus_bot_new_lead import get_sf_access_token

Router = APIRouter()

# --- GOOGLE SHEETS SETUP ---
def get_sheets_client():
    """Authenticates using service account JSON from env variable."""
    
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    # Load JSON from Railway env variable
    service_account_info = json.loads(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    )

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        service_account_info,
        scope
    )

    return gspread.authorize(creds)

@Router.post("/post-call")
async def handle_post_call(request: Request):
    try:
        data = await request.json()
        print(data)

        # ✅ FIX: everything is inside "data"
        payload = data.get("data", {})

        # 1. Extract Metadata from ElevenLabs Webhook
        metadata = payload.get("metadata", {})
        duration = int(metadata.get("call_duration_secs", 0))
        call_status = payload.get("status", "unknown")

        # ✅ FIX: transcript is a list → convert to string
        transcript = payload.get("transcript", [])
        import json
        transcript_str = json.dumps(transcript) if transcript else "No transcript available"
        
        # Extract variables passed during call initiation
        custom_data = payload.get("conversation_initiation_client_data", {}).get("dynamic_variables", {})
        lead_id = custom_data.get("lead_id")

        # ✅ FIX: conversation_id is inside payload
        conv_id = payload.get("conversation_id")

        if not lead_id:
            logging.error("Post-Call received but no lead_id found.")
            return {"status": "error", "message": "No lead_id"}

        # 2. Salesforce Update & Data Enrichment
        access_token = await get_sf_access_token()
        sf_headers = {"Authorization": f"Bearer {access_token}"}
        
        # First, update the lead with the final transcript and duration
        sf_payload = {
            "Call_Duration__c": f"{duration} seconds",
            "Call_Status__c": call_status,
            "Call_Transcript__c": transcript_str  # ✅ FIX applied
        }

        async with httpx.AsyncClient() as client:
            # PATCH: Update transcript in Salesforce
            update_url = f"{SF_INSTANCE_URL}/services/data/v57.0/sobjects/Lead/{lead_id}"
            await client.patch(update_url, json=sf_payload, headers=sf_headers)
            
            # GET: Fetch latest Lead info (to get Name, ACQ Manager, and Tool Results)
            lead_res = await client.get(update_url, headers=sf_headers)
            lead_info = lead_res.json()

        # 3. Discovery Call Logic (Only logs to Sheets if call > 3 minutes)
        if duration > 180:
            logging.info(f"Call {conv_id} lasted {duration}s. Logging to Google Sheets.")
            
            # Open the spreadsheet and specific worksheet
            gs_client = get_sheets_client()
            sheet = gs_client.open("Ai Bot FUS Discovery Call List").worksheet("Call Recording Metrics")
            
            # Prepare the row exactly as your old Zapier workflow did
            row = [
                conv_id,
                lead_info.get("Name", "N/A"),
                lead_info.get("Who_manages_the_property__c"),
                lead_info.get("Address", "N/A"),
                f"{duration}s",
                lead_info.get("Change_of_Mind_Reason__c"),
                lead_info.get("Is_Interested_in_Selling__c"),
                lead_info.get("Check_Back_Time__c"),
                f"https://leftmain-4606.lightning.force.com/lightning/r/Lead/{lead_id}/view"
            ]
            
            sheet.append_row(row)
            logging.info(f"Successfully added row to Google Sheet for lead {lead_id}")

        return {"status": "success", "duration": duration}

    except Exception as e:
        logging.error(f"Post-Call Critical Error: {str(e)}")
        return {"status": "error", "detail": str(e)}