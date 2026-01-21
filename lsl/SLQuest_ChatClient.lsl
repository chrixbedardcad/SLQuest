string SERVER_BASE = "http://slquest.duckdns.org:8001";
string NPC_ID = "SLQuest_DefaultNPC";
string PROFILE_PAGE_BASE = "https://world.secondlife.com/resident/";
// NOTE: Place SLQuest_CallbackReceiver.lsl in the same linkset so callbacks can be routed.
integer SESSION_TIMEOUT_SEC = 90;
integer IDLE_HINT_COOLDOWN_SEC = 45;
integer DEBUG = TRUE;
integer DEBUG_PROFILE_FACE = ALL_SIDES;
integer GREET_ENABLED = FALSE;
float GREET_RANGE = 12.0;
float GREET_INTERVAL = 10.0;
integer ALLOW_WEB_SEARCH = TRUE;
integer GREET_COOLDOWN_PER_AVATAR_SEC = 300;
integer GREET_GLOBAL_COOLDOWN_SEC = 20;
integer GREET_SKIP_OWNER = TRUE;
integer LM_CB_TOKEN = 9100;
integer LM_CB_REPLY = 9101;
integer LM_ACTION = 9200;
integer FETCH_MAX = 16384;

list gActiveAvatars = [];
list gSessionEndTimes = [];
list gQueuedMessages = [];
list gInFlightAvatars = [];
list gRequestMap = [];
list gFetchMap = [];
integer gListen = -1;
integer gNextHintTime = 0;
integer gNextGlobalGreetAt = 0;
list gGreetMemory = [];
key gProfileRequest = NULL_KEY;
key gProfileAvatar = NULL_KEY;
integer gProfileClearAt = 0;
string gCallbackToken = "";

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

list pipeSplit(string s)
{
    return llParseString2List(s, ["|"], []);
}

string pipeGet(list segs, string key)
{
    integer i;
    string prefix = key + "=";
    for (i = 0; i < llGetListLength(segs); ++i)
    {
        string seg = llList2String(segs, i);
        if (llSubStringIndex(seg, prefix) == 0)
        {
            return llUnescapeURL(llGetSubString(seg, llStringLength(prefix), -1));
        }
    }
    return "";
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
    gActiveAvatars = [];
    gSessionEndTimes = [];
    gQueuedMessages = [];
    gInFlightAvatars = [];
    gRequestMap = [];
    gFetchMap = [];
    scheduleNextHint();
    gProfileRequest = NULL_KEY;
    gProfileAvatar = NULL_KEY;
    gProfileClearAt = 0;
}

integer findActiveIndex(key avatar)
{
    return llListFindList(gActiveAvatars, [avatar]);
}

integer isActive(key avatar)
{
    return findActiveIndex(avatar) != -1;
}

integer isInFlight(key avatar)
{
    return llListFindList(gInFlightAvatars, [avatar]) != -1;
}

setQueuedMessage(key avatar, string message)
{
    integer index = findActiveIndex(avatar);
    if (index == -1)
    {
        return;
    }
    gQueuedMessages = llListReplaceList(gQueuedMessages, [message], index, index);
}

string getQueuedMessage(key avatar)
{
    integer index = findActiveIndex(avatar);
    if (index == -1)
    {
        return "";
    }
    return llList2String(gQueuedMessages, index);
}

clearQueuedMessage(key avatar)
{
    setQueuedMessage(avatar, "");
}

handlePipePackage(string body)
{
    list segs = pipeSplit(body);
    string msgType = pipeGet(segs, "TYPE");
    if (msgType != "PKG")
    {
        return;
    }
    key avatar = (key)pipeGet(segs, "USER");
    string chat = pipeGet(segs, "CHAT");
    string act = pipeGet(segs, "ACT");
    if (avatar == NULL_KEY)
    {
        return;
    }
    integer inflightIndex = llListFindList(gInFlightAvatars, [avatar]);
    if (inflightIndex != -1)
    {
        gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
    }
    if (!isActive(avatar))
    {
        return;
    }
    updateDebugTexture(avatar);
    if (chat != "")
    {
        llRegionSayTo(avatar, 0, chat);
    }
    if (act != "")
    {
        llMessageLinked(LINK_SET, LM_ACTION, act, avatar);
    }
    string queued = getQueuedMessage(avatar);
    if (queued != "")
    {
        clearQueuedMessage(avatar);
        sendMessage(avatar, queued);
    }
}

string buildPayload(key avatar, string message, string clientReqId)
{
    return llList2Json(JSON_OBJECT, [
        "npc_id", NPC_ID,
        "avatar_key", (string)avatar,
        "avatar_name", llGetUsername(avatar),
        "avatar_display_name", llGetDisplayName(avatar),
        "avatar_username", llGetUsername(avatar),
        "object_key", (string)llGetKey(),
        "object_name", llGetObjectName(),
        "region", llGetRegionName(),
        "allow_web_search", ALLOW_WEB_SEARCH,
        "message", message,
        "client_req_id", clientReqId,
        "callback_token", gCallbackToken,
        "ts", llGetTimestamp()
    ]);
}

sendMessage(key avatar, string message)
{
    string clientReqId = (string)llGenerateKey();
    string payload = buildPayload(avatar, message, clientReqId);
    string url = SERVER_BASE + "/chat";
    integer isAsync = FALSE;
    if (gCallbackToken != "")
    {
        url = SERVER_BASE + "/chat_async";
        isAsync = TRUE;
    }
    gInFlightAvatars += [avatar];
    key requestId = llHTTPRequest(url, [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"], payload);
    gRequestMap += [requestId, avatar, isAsync];
}

ensureListen()
{
    if (gListen != -1)
    {
        return;
    }
    gListen = llListen(0, "", NULL_KEY, "");
}

startSession(key avatar)
{
    if (isActive(avatar))
    {
        return;
    }
    gActiveAvatars += [avatar];
    gSessionEndTimes += [nowUnix() + SESSION_TIMEOUT_SEC];
    gQueuedMessages += [""];
    ensureListen();
    llRegionSayTo(avatar, 0, "Chat started. Say something in public chat near me.");
    updateDebugTexture(avatar);
    gProfileClearAt = 0;
}

endSession(key avatar, string message)
{
    integer index = findActiveIndex(avatar);
    if (index == -1)
    {
        return;
    }
    gActiveAvatars = llDeleteSubList(gActiveAvatars, index, index);
    gSessionEndTimes = llDeleteSubList(gSessionEndTimes, index, index);
    gQueuedMessages = llDeleteSubList(gQueuedMessages, index, index);
    integer inflightIndex = llListFindList(gInFlightAvatars, [avatar]);
    if (inflightIndex != -1)
    {
        gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
    }
    llRegionSayTo(avatar, 0, message);
    if (llGetListLength(gActiveAvatars) == 0 && gListen != -1)
    {
        llListenRemove(gListen);
        gListen = -1;
        scheduleNextHint();
        gProfileClearAt = nowUnix() + IDLE_HINT_COOLDOWN_SEC;
    }
}

default
{
    state_entry()
    {
        resetSession();
        llSetTimerEvent(1.0);
        if (GREET_ENABLED)
        {
            llSensorRepeat("", NULL_KEY, AGENT, GREET_RANGE, PI, GREET_INTERVAL);
        }
    }

    touch_start(integer total_number)
    {
        key toucher = llDetectedKey(0);
        if (isActive(toucher))
        {
            endSession(toucher, "Session ended. Touch me again to talk.");
            return;
        }
        startSession(toucher);
    }

    listen(integer channel, string name, key id, string message)
    {
        if (!isActive(id))
        {
            return;
        }

        integer index = findActiveIndex(id);
        if (index == -1)
        {
            return;
        }
        gSessionEndTimes = llListReplaceList(gSessionEndTimes, [nowUnix() + SESSION_TIMEOUT_SEC], index, index);

        if (isInFlight(id))
        {
            setQueuedMessage(id, message);
            return;
        }

        sendMessage(id, message);
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
        integer fetchIndex = llListFindList(gFetchMap, [request_id]);
        if (fetchIndex != -1)
        {
            key avatar = llList2Key(gFetchMap, fetchIndex + 1);
            gFetchMap = llDeleteSubList(gFetchMap, fetchIndex, fetchIndex + 1);
            if (status == 200)
            {
                handlePipePackage(body);
            }
            else
            {
                integer inflightIndex = llListFindList(gInFlightAvatars, [avatar]);
                if (inflightIndex != -1)
                {
                    gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
                }
                if (isActive(avatar))
                {
                    llRegionSayTo(avatar, 0, "Sorry, fetch failed. Try again.");
                }
                string queuedFail = getQueuedMessage(avatar);
                if (queuedFail != "")
                {
                    clearQueuedMessage(avatar);
                    sendMessage(avatar, queuedFail);
                }
            }
            return;
        }
        integer requestIndex = llListFindList(gRequestMap, [request_id]);
        if (requestIndex == -1)
        {
            return;
        }
        key activeAvatar = llList2Key(gRequestMap, requestIndex + 1);
        integer isAsync = llList2Integer(gRequestMap, requestIndex + 2);
        gRequestMap = llDeleteSubList(gRequestMap, requestIndex, requestIndex + 2);
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

        if (!isActive(activeAvatar))
        {
            return;
        }

        if (isAsync)
        {
            string okValue = llJsonGetValue(body, ["ok"]);
            if (status != 200 || okValue != "true")
            {
                integer inflightIndex = llListFindList(gInFlightAvatars, [activeAvatar]);
                if (inflightIndex != -1)
                {
                    gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
                }
                updateDebugTexture(activeAvatar);
                llRegionSayTo(activeAvatar, 0, "ERROR Status: " + (string) status + ": Sorry, I glitched. Try again.");
                string queued = getQueuedMessage(activeAvatar);
                if (queued != "")
                {
                    clearQueuedMessage(activeAvatar);
                    sendMessage(activeAvatar, queued);
                }
            }
            return;
        }

        integer inflightIndex = llListFindList(gInFlightAvatars, [activeAvatar]);
        if (inflightIndex != -1)
        {
            gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
        }

        if (status != 200)
        {
            updateDebugTexture(activeAvatar);
            llRegionSayTo(activeAvatar, 0, "ERROR Status: " + (string) status + ": Sorry, I glitched. Try again.");
        }
        else
        {
            if (replyType == JSON_INVALID)
            {
                string okValue = llJsonGetValue(body, ["ok"]);
                if (okValue == JSON_INVALID)
                {
                    updateDebugTexture(activeAvatar);
                    llRegionSayTo(activeAvatar, 0, "ERROR Invalid JSON response: Sorry, I glitched. Try again.");
                }
                else
                {
                    updateDebugTexture(activeAvatar);
                    llRegionSayTo(activeAvatar, 0, "ERROR Missing reply in server response: Sorry, I glitched. Try again.");
                }
            }
            else
            {
                updateDebugTexture(activeAvatar);
               // llRegionSayTo(activeAvatar, 0, reply);
               llSay(0,llGetDisplayName(activeAvatar) +": " + reply);
                
            }
        }

        string queued = getQueuedMessage(activeAvatar);
        if (queued != "")
        {
            clearQueuedMessage(activeAvatar);
            sendMessage(activeAvatar, queued);
        }
    }

    link_message(integer sender, integer num, string str, key id)
    {
        if (num == LM_CB_TOKEN)
        {
            gCallbackToken = str;
            return;
        }
        if (num != LM_CB_REPLY)
        {
            return;
        }
        if (llGetSubString(str, 0, 0) == "{")
        {
            key avatar = (key)llJsonGetValue(str, ["avatar_key"]);
            string reply = llJsonGetValue(str, ["reply"]);
            if (avatar == NULL_KEY || reply == "")
            {
                return;
            }
            integer inflightIndex = llListFindList(gInFlightAvatars, [avatar]);
            if (inflightIndex != -1)
            {
                gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
            }
            if (!isActive(avatar))
            {
                return;
            }
            updateDebugTexture(avatar);
            llRegionSayTo(avatar, 0, reply);
            string queued = getQueuedMessage(avatar);
            if (queued != "")
            {
                clearQueuedMessage(avatar);
                sendMessage(avatar, queued);
            }
            return;
        }
        list segs = pipeSplit(str);
        string msgType = pipeGet(segs, "TYPE");
        if (msgType == "FETCH")
        {
            key avatar = (key)pipeGet(segs, "USER");
            string token = pipeGet(segs, "TOKEN");
            if (avatar != NULL_KEY && token != "")
            {
                string url = SERVER_BASE + "/sl/fetch?token=" + llEscapeURL(token);
                key req = llHTTPRequest(url, [HTTP_METHOD, "GET", HTTP_BODY_MAXLENGTH, FETCH_MAX], "");
                gFetchMap += [req, avatar];
            }
            return;
        }
        if (msgType == "PKG")
        {
            handlePipePackage(str);
            return;
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
        integer count = llGetListLength(gActiveAvatars);
        integer index;
        for (index = count - 1; index >= 0; --index)
        {
            integer sessionEnd = llList2Integer(gSessionEndTimes, index);
            if (now >= sessionEnd)
            {
                key avatar = llList2Key(gActiveAvatars, index);
                endSession(avatar, "Session ended. Touch me again to talk.");
            }
        }
        if (llGetListLength(gActiveAvatars) > 0)
        {
            return;
        }
        if (gProfileClearAt > 0 && now >= gProfileClearAt)
        {
            updateDebugTexture(NULL_KEY);
            gProfileClearAt = 0;
        }

        if (now >= gNextHintTime)
        {
            llSay(0, "Touch me if you want to talk.");
            scheduleNextHint();
        }
    }

    sensor(integer total_number)
    {
        if (llGetListLength(gActiveAvatars) > 0)
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
