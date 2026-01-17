string SERVER_BASE = "http://slquest.duckdns.org:8001";
string NPC_ID = "SLQuest_DefaultNPC";
string PROFILE_PAGE_BASE = "https://world.secondlife.com/resident/";
integer SESSION_TIMEOUT_SEC = 90;
integer IDLE_HINT_COOLDOWN_SEC = 45;
integer DEBUG = TRUE;
integer DEBUG_PROFILE_FACE = 0;
integer GREET_ENABLED = TRUE;
float GREET_RANGE = 12.0;
float GREET_INTERVAL = 10.0;
integer ALLOW_WEB_SEARCH = TRUE;
integer GREET_COOLDOWN_PER_AVATAR_SEC = 300;
integer GREET_GLOBAL_COOLDOWN_SEC = 20;
integer GREET_SKIP_OWNER = TRUE;

key gActiveAvatar = NULL_KEY;
integer gListen = -1;
integer gInFlight = FALSE;
string gQueuedMessage = "";
integer gSessionEndTime = 0;
integer gNextHintTime = 0;
integer gNextGlobalGreetAt = 0;
list gGreetMemory = [];
key gProfileRequest = NULL_KEY;
key gProfileAvatar = NULL_KEY;

string extractProfileImageKey(string body)
{
    string profile_key_prefix = "<meta name=\"imageid\" content=\"";
    integer startIndex = llSubStringIndex(body, profile_key_prefix);
    if (startIndex == -1)
    {
        return "";
    }
    startIndex += llStringLength(profile_key_prefix);
    integer endIndex = llSubStringIndex(llGetSubString(body, startIndex, -1), "\"");
    if (endIndex == -1)
    {
        return "";
    }
    return llGetSubString(body, startIndex, startIndex + endIndex - 1);
}

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

updateDebugTexture(key avatar)
{
    if (!DEBUG)
    {
        return;
    }
    if (avatar == NULL_KEY)
    {
        gProfileRequest = NULL_KEY;
        gProfileAvatar = NULL_KEY;
        llSetTexture(TEXTURE_BLANK, DEBUG_PROFILE_FACE);
        return;
    }
    gProfileAvatar = avatar;
    gProfileRequest = llHTTPRequest(
        PROFILE_PAGE_BASE + (string)avatar,
        [HTTP_METHOD, "GET"],
        ""
    );
}

integer findGreetIndex(key avatar)
{
    return llListFindList(gGreetMemory, [avatar]);
}

integer canGreet(key avatar, integer now)
{
    integer index = findGreetIndex(avatar);
    if (index == -1)
    {
        return TRUE;
    }
    integer lastGreeted = llList2Integer(gGreetMemory, index + 1);
    if ((now - lastGreeted) >= GREET_COOLDOWN_PER_AVATAR_SEC)
    {
        return TRUE;
    }
    return FALSE;
}

markGreeted(key avatar, integer now)
{
    integer index = findGreetIndex(avatar);
    if (index == -1)
    {
        gGreetMemory += [avatar, now];
        return;
    }
    gGreetMemory = llListReplaceList(gGreetMemory, [avatar, now], index, index + 1);
}

pruneOldGreets(integer now)
{
    integer count = llGetListLength(gGreetMemory);
    integer index;
    list pruned = [];
    for (index = 0; index < count; index += 2)
    {
        integer lastGreeted = llList2Integer(gGreetMemory, index + 1);
        if ((now - lastGreeted) < GREET_COOLDOWN_PER_AVATAR_SEC)
        {
            pruned += [llList2Key(gGreetMemory, index), lastGreeted];
        }
    }
    gGreetMemory = pruned;
}

resetSession()
{
    updateDebugTexture(NULL_KEY);
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
    gProfileRequest = NULL_KEY;
    gProfileAvatar = NULL_KEY;
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
        "allow_web_search", ALLOW_WEB_SEARCH,
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
    updateDebugTexture(avatar);
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
        if (GREET_ENABLED)
        {
            llSensorRepeat("", NULL_KEY, AGENT, GREET_RANGE, PI, GREET_INTERVAL);
        }
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

        gSessionEndTime = nowUnix() + SESSION_TIMEOUT_SEC;

        if (gInFlight)
        {
            gQueuedMessage = message;
            return;
        }

        sendMessage(message);
    }

    http_response(key request_id, integer status, list metadata, string body)
    {
        if (request_id == gProfileRequest)
        {
            gProfileRequest = NULL_KEY;
            if (status != 200 || gProfileAvatar == NULL_KEY)
            {
                llSetTexture(TEXTURE_BLANK, DEBUG_PROFILE_FACE);
                return;
            }
            string profileKey = extractProfileImageKey(body);
            if (profileKey == "")
            {
                llSetTexture(TEXTURE_BLANK, DEBUG_PROFILE_FACE);
                return;
            }
            llSetTexture((key)profileKey, DEBUG_PROFILE_FACE);
            return;
        }
        gInFlight = FALSE;
        string replyType = llJsonValueType(body, ["reply"]);
        string reply = "";
        if (replyType == JSON_STRING)
        {
            reply = llJsonGetValue(body, ["reply"]);
        }

        if (DEBUG)
        {
            string debugBody = body;
            integer bodyLength = llStringLength(debugBody);
            if (bodyLength > 300)
            {
                debugBody = llGetSubString(debugBody, 0, 299) + "...";
            }
            llOwnerSay(debugBody);
        }

        if (gActiveAvatar == NULL_KEY)
        {
            gQueuedMessage = "";
            return;
        }

        if (status != 200)
        {
            llRegionSayTo(gActiveAvatar, 0, "ERROR Status: " + (string) status + ": Sorry, I glitched. Try again.");
        }
        else
        {
            if (replyType == JSON_INVALID)
            {
                string okValue = llJsonGetValue(body, ["ok"]);
                if (okValue == JSON_INVALID)
                {
                    llRegionSayTo(gActiveAvatar, 0, "ERROR Invalid JSON response: Sorry, I glitched. Try again.");
                }
                else
                {
                    llRegionSayTo(gActiveAvatar, 0, "ERROR Missing reply in server response: Sorry, I glitched. Try again.");
                }
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

    dataserver(key query_id, string data)
    {
        if (query_id != gProfileRequest)
        {
            return;
        }
        key profileTexture = (key)data;
        if (profileTexture == NULL_KEY)
        {
            profileTexture = TEXTURE_BLANK;
        }
        if (gProfileAvatar != NULL_KEY)
        {
            llSetTexture(profileTexture, DEBUG_PROFILE_FACE);
        }
        gProfileRequest = NULL_KEY;
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

    sensor(integer total_number)
    {
        if (gActiveAvatar != NULL_KEY)
        {
            return;
        }
        integer now = nowUnix();
        if (now < gNextGlobalGreetAt)
        {
            return;
        }
        pruneOldGreets(now);
        integer index;
        for (index = 0; index < total_number; ++index)
        {
            key avatar = llDetectedKey(index);
            if (!(GREET_SKIP_OWNER && avatar == llGetOwner()) && canGreet(avatar, now))
            {
                llSay(0, "Hi " + llDetectedName(index) + ", touch me if you want to talk!");
                markGreeted(avatar, now);
                gNextGlobalGreetAt = now + GREET_GLOBAL_COOLDOWN_SEC;
                return;
            }
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
