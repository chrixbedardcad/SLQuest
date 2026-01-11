string SERVER_BASE = "http://slquest.duckdns.org:8001";
string NPC_ID = "SLQuest_DefaultNPC";
integer SESSION_TIMEOUT_SEC = 90;

key gAvatar = NULL_KEY;
integer gListen = -1;
integer gInFlight = FALSE;
string gQueuedMessage = "";

resetSession()
{
    if (gListen != -1)
    {
        llListenRemove(gListen);
        gListen = -1;
    }
    gAvatar = NULL_KEY;
    gInFlight = FALSE;
    gQueuedMessage = "";
    llSetTimerEvent(0.0);
}

string buildPayload(string message)
{
    return llList2Json(JSON_OBJECT, [
        "npc_id", NPC_ID,
        "avatar_key", (string)gAvatar,
        "avatar_name", llKey2Name(gAvatar),
        "object_key", (string)llGetKey(),
        "object_name", llGetObjectName(),
        "region", llGetRegionName(),
        "message", message,
        "ts", llGetTimestamp()
    ]);
}

sendMessage(string message)
{
    string payload = buildPayload(message);
    string url = SERVER_BASE + "/chat";
    gInFlight = TRUE;
    llHTTPRequest(url, [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"], payload);
}

default
{
    state_entry()
    {
        resetSession();
    }

    touch_start(integer total_number)
    {
        key toucher = llDetectedKey(0);
        resetSession();
        gAvatar = toucher;
        gListen = llListen(0, "", gAvatar, "");
        llSetTimerEvent((float)SESSION_TIMEOUT_SEC);
        llRegionSayTo(gAvatar, 0, "Chat started. Talk in public chat near me.");
    }

    listen(integer channel, string name, key id, string message)
    {
        if (id != gAvatar)
        {
            return;
        }

        if (gInFlight)
        {
            gQueuedMessage = message;
            return;
        }

        sendMessage(message);
    }

    http_response(key request_id, integer status, list metadata, string body)
    {
        gInFlight = FALSE;

        if (status != 200)
        {
            llRegionSayTo(gAvatar, 0, "Sorry, I glitched. Try again.");
        }
        else
        {
            string reply = llJsonGetValue(body, ["reply"]);
            if (reply == JSON_INVALID)
            {
                llRegionSayTo(gAvatar, 0, "Sorry, I glitched. Try again.");
            }
            else
            {
                llRegionSayTo(gAvatar, 0, reply);
            }
        }

        if (gQueuedMessage != "")
        {
            string queued = gQueuedMessage;
            gQueuedMessage = "";
            sendMessage(queued);
        }
    }

    timer()
    {
        if (gAvatar != NULL_KEY)
        {
            llRegionSayTo(gAvatar, 0, "Session ended.");
        }
        resetSession();
    }

    changed(integer change)
    {
        if (change & (CHANGED_OWNER | CHANGED_REGION | CHANGED_INVENTORY))
        {
            resetSession();
        }
    }

    on_rez(integer start_param)
    {
        resetSession();
    }
}
