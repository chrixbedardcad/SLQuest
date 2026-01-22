// SLQuest NPC Notecard Sync Example
// Reads a notecard named "system.md" and pushes it to the SLQuest server.
// NOTE: Notecard lines are limited to ~1024 bytes each; wrap long text across multiple lines.
// NOTE: Do NOT expect large responses in LSL; the server should return short JSON only.

string SERVER_URL = "http://slquest.duckdns.org:8001";
string ADMIN_TOKEN = "";
string NPC_ID = "";
string DISPLAY_NAME = "";
string MODEL = "gpt-5.2";
integer MAX_HISTORY_EVENTS = 12;
string NOTECARD = "system.md";

key gNotecardQuery;
integer gLineIndex = 0;
string gNotecardText = "";
key gNotecardKey = NULL_KEY;

string getConfigValue(string key, string fallback)
{
    string desc = llGetObjectDesc();
    if (desc == "")
    {
        return fallback;
    }
    list parts = llParseString2List(desc, [" ", "\n", "\t", "|", ";"], []);
    integer i;
    string prefix = key + "=";
    for (i = 0; i < llGetListLength(parts); ++i)
    {
        string part = llList2String(parts, i);
        if (llSubStringIndex(part, prefix) == 0)
        {
            return llGetSubString(part, llStringLength(prefix), -1);
        }
    }
    return fallback;
}

string getServerUrl()
{
    return getConfigValue("SERVER_URL", SERVER_URL);
}

key getRootKey()
{
    return llGetLinkKey(LINK_ROOT);
}

string getNpcId()
{
    if (NPC_ID != "")
    {
        return NPC_ID;
    }
    return llGetObjectName();
}

string buildPayload()
{
    string region = llGetRegionName();
    return llList2Json(JSON_OBJECT, [
        "admin_token", ADMIN_TOKEN,
        "npc_id", getNpcId(),
        "display_name", DISPLAY_NAME,
        "model", MODEL,
        "max_history_events", MAX_HISTORY_EVENTS,
        "system_prompt", gNotecardText,
        "source", llList2Json(JSON_OBJECT, [
            "object_key", (string)getRootKey(),
            "owner_key", (string)llGetOwner(),
            "region", region
        ])
    ]);
}

sendUpdate()
{
    string payload = buildPayload();
    list headers = [
        HTTP_METHOD, "POST",
        HTTP_MIMETYPE, "application/json;charset=utf-8",
        HTTP_BODY_MAXLENGTH, 4096
    ];
    llHTTPRequest(getServerUrl() + "/admin/npc/upsert", headers, payload);
}

startNotecardRead()
{
    if (llGetInventoryType(NOTECARD) != INVENTORY_NOTECARD)
    {
        llOwnerSay("Notecard not found: " + NOTECARD);
        return;
    }
    gNotecardText = "";
    gLineIndex = 0;
    gNotecardQuery = llGetNotecardLine(NOTECARD, gLineIndex);
}

integer isOwner(key id)
{
    return id == llGetOwner();
}

default
{
    state_entry()
    {
        gNotecardKey = llGetInventoryKey(NOTECARD);
        startNotecardRead();
    }

    changed(integer change)
    {
        if (change & CHANGED_INVENTORY)
        {
            key newKey = llGetInventoryKey(NOTECARD);
            if (newKey != gNotecardKey)
            {
                llResetScript();
            }
        }
    }

    touch_start(integer count)
    {
        if (isOwner(llDetectedKey(0)))
        {
            llOwnerSay("Manual NPC sync requested.");
            llResetScript();
        }
        else
        {
            llOwnerSay("Only the owner can resync this NPC.");
        }
    }

    dataserver(key queryId, string data)
    {
        if (queryId != gNotecardQuery)
        {
            return;
        }
        if (data == EOF)
        {
            sendUpdate();
            return;
        }
        if (gNotecardText != "")
        {
            gNotecardText += "\n";
        }
        gNotecardText += data;
        gLineIndex += 1;
        gNotecardQuery = llGetNotecardLine(NOTECARD, gLineIndex);
    }

    http_response(key request_id, integer status, list metadata, string body)
    {
        if (status >= 200 && status < 300)
        {
            llOwnerSay("NPC sync success: " + (string)status);
        }
        else
        {
            llOwnerSay("NPC sync failed: " + (string)status + " " + body);
        }
    }
}
