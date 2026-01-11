string SERVER_BASE = "http://slquest.duckdns.org:8001";
string NPC_ID = "SLQuest_DefaultNPC";
integer SESSION_TIMEOUT_SEC = 90;
integer IDLE_HINT_COOLDOWN_SEC = 45;

key gActiveAvatar = NULL_KEY;
integer gListen = -1;
integer gInFlight = FALSE;
string gQueuedMessage = "";
integer gSessionEndTime = 0;
integer gNextHintTime = 0;

integer nowUnix()
{
    return llGetUnixTime();
}

integer computeNextHintDelay()
{
    integer interval = 30 + (integer)llFrand(31.0);
    if (interval < IDLE_HINT_COOLDOWN_SEC)
    {
        interval = IDLE_HINT_COOLDOWN_SEC;
    }
    return interval;
}

scheduleNextHint()
{
    gNextHintTime = nowUnix() + computeNextHintDelay();
}

resetSession()
{
    if (gListen != -1)
    {
        llListenRemove(gListen);
        gListen = -1;
    }
    gActiveAvatar = NULL_KEY;
    gInFlight = FALSE;
    gQueuedMessage = "";
    gSessionEndTime = 0;
    scheduleNextHint();
}

string buildPayload(string message)
{
    return llList2Json(JSON_OBJECT, [
        "npc_id", NPC_ID,
        "avatar_key", (string)gActiveAvatar,
        "avatar_name", llKey2Name(gActiveAvatar),
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

startSession(key avatar)
{
    gActiveAvatar = avatar;
    gListen = llListen(0, "", gActiveAvatar, "");
    gSessionEndTime = nowUnix() + SESSION_TIMEOUT_SEC;
    llRegionSayTo(gActiveAvatar, 0, "Chat started. Say something in public chat near me.");
}

endSession(string message)
{
    key oldAvatar = gActiveAvatar;
    resetSession();
    if (oldAvatar != NULL_KEY)
    {
        llRegionSayTo(oldAvatar, 0, message);
    }
}

default
{
    state_entry()
    {
        resetSession();
        llSetTimerEvent(5.0);
    }

    touch_start(integer total_number)
    {
        key toucher = llDetectedKey(0);
        if (gActiveAvatar != NULL_KEY)
        {
            if (toucher == gActiveAvatar)
            {
                endSession("Session ended. Touch me again to talk.");
            }
            else
            {
                llRegionSayTo(toucher, 0, "I'm busy right now. Please try again soon.");
            }
            return;
        }

        startSession(toucher);
    }

    listen(integer channel, string name, key id, string message)
    {
        if (id != gActiveAvatar)
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

        if (gActiveAvatar == NULL_KEY)
        {
            gQueuedMessage = "";
            return;
        }

        if (status != 200)
        {
            llRegionSayTo(gActiveAvatar, 0, "Sorry, I glitched. Try again.");
        }
        else
        {
            string reply = llJsonGetValue(body, ["reply"]);
            if (reply == JSON_INVALID)
            {
                llRegionSayTo(gActiveAvatar, 0, "Sorry, I glitched. Try again.");
            }
            else
            {
                llRegionSayTo(gActiveAvatar, 0, reply);
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
        integer now = nowUnix();
        if (gActiveAvatar != NULL_KEY)
        {
            if (now >= gSessionEndTime)
            {
                endSession("Session ended. Touch me again to talk.");
            }
            return;
        }

        if (now >= gNextHintTime)
        {
            llSay(0, "Touch me if you want to talk.");
            scheduleNextHint();
        }
    }

    changed(integer change)
    {
        if (change & CHANGED_REGION_START)
        {
            resetSession();
        }
    }

    on_rez(integer start_param)
    {
        resetSession();
    }
}
