string SERVER_BASE = "http://slquest.duckdns.org:8001";
string SERVER_BASE_FALLBACK = "";
string NPC_ID = "";
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
integer LM_CB_REFRESH = 9102;
integer LM_ACTION = 9200;
integer FETCH_MAX = 16384;
string DEBUG_TAG = "SLQuest Debug: ";
integer ASYNC_ONLY = TRUE;
integer ASYNC_WAIT_NOTICE_SEC = 8;
integer ASYNC_WAIT_COOLDOWN_SEC = 20;
integer USE_PUBLIC_SAY = TRUE;
integer MIN_FREE_MEMORY = 4096;
integer MAX_REQUEST_MAP_REQUESTS = 30;
integer MAX_FETCH_MAP = 30;
integer HTTP_REQUEST_TIMEOUT_SEC = 90;
integer PRUNE_INTERVAL_SEC = 30;
integer gLastPruneTime = 0;

list gActiveAvatars = [];
list gSessionEndTimes = [];
list gQueuedMessages = [];
list gInFlightAvatars = [];
list gRequestMap = [];
list gFetchMap = [];
list gPendingAvatars = [];
list gPendingStartTimes = [];
list gPendingLastNotice = [];
integer gListen = -1;
integer gNextHintTime = 0;
integer gNextGlobalGreetAt = 0;
list gGreetMemory = [];
key gProfileRequest = NULL_KEY;
key gProfileAvatar = NULL_KEY;
integer gProfileClearAt = 0;
string gCallbackToken = "";

debugTrace(string message)
{
    if (!DEBUG)
    {
        return;
    }
    llOwnerSay(DEBUG_TAG + message);
}

key getRootKey()
{
    return llGetLinkKey(LINK_ROOT);
}

string getConfigValue(string variable_key_name, string fallback)
{
    string desc = llGetObjectDesc();
    if (desc == "")
    {
        return fallback;
    }
    list parts = llParseString2List(desc, [" ", "\n", "\t", "|", ";"], []);
    integer i;
    string prefix = variable_key_name + "=";
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

string getServerBase()
{
    return getConfigValue("SERVER_BASE", SERVER_BASE);
}

string getServerBaseFallback()
{
    return getConfigValue("SERVER_BASE_FALLBACK", SERVER_BASE_FALLBACK);
}

integer usePublicSay()
{
    string value = getConfigValue("PUBLIC_SAY", "");
    if (value != "")
    {
        return ((integer)value != 0);
    }
    return USE_PUBLIC_SAY;
}

sayTo(key avatar, string message)
{
    if (usePublicSay())
    {
        llSay(0, message);
        return;
    }
    llRegionSayTo(avatar, 0, message);
}

string getNpcId()
{
    if (NPC_ID != "")
    {
        return NPC_ID;
    }
    return llGetObjectName();
}

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

// Avoid list parsing to reduce stack-heap collision risk with large payloads.
string pipeGetValue(string s, string key_name)
{
    string needle = "|" + key_name + "=";
    integer pos = llSubStringIndex(s, needle);

    integer start;
    if (pos == -1)
    {
        string needle0 = key_name + "=";
        if (llSubStringIndex(s, needle0) != 0)
        {
            return "";
        }
        start = llStringLength(needle0);
    }
    else
    {
        start = pos + llStringLength(needle);
    }

    integer endRel = llSubStringIndex(llGetSubString(s, start, -1), "|");
    if (endRel == -1)
    {
        return llUnescapeURL(llGetSubString(s, start, -1));
    }
    return llUnescapeURL(llGetSubString(s, start, start + endRel - 1));
}

integer nowUnix()
{
    return llGetUnixTime();
}

integer ensureMemory(string context)
{
    integer freeMemory = llGetFreeMemory();
    if (freeMemory >= MIN_FREE_MEMORY)
    {
        return TRUE;
    }
    llOwnerSay("Low memory (" + (string)freeMemory + ") during " + context + ". Resetting sessions.");
    resetSession();
    return FALSE;
}

integer ensureRequestCapacity()
{
    integer requestCount = llGetListLength(gRequestMap) / 7;
    if (requestCount <= MAX_REQUEST_MAP_REQUESTS)
    {
        return TRUE;
    }
    llOwnerSay("Request backlog exceeded; resetting sessions.");
    resetSession();
    return FALSE;
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

pruneStaleRequests(integer now)
{
    integer count = llGetListLength(gRequestMap);
    integer index;
    list kept = [];
    for (index = 0; index < count; index += 7)
    {
        integer timestamp = llList2Integer(gRequestMap, index + 6);
        if ((now - timestamp) < HTTP_REQUEST_TIMEOUT_SEC)
        {
            kept += llList2List(gRequestMap, index, index + 6);
        }
        else
        {
            key avatar = llList2Key(gRequestMap, index + 1);
            debugTrace("Pruning stale request for avatar=" + (string)avatar);
            integer inflightIndex = llListFindList(gInFlightAvatars, [avatar]);
            if (inflightIndex != -1)
            {
                gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
            }
            clearPending(avatar);
        }
    }
    gRequestMap = kept;
}

pruneStaleFetches(integer now)
{
    integer count = llGetListLength(gFetchMap);
    integer index;
    list kept = [];
    for (index = 0; index < count; index += 3)
    {
        integer timestamp = llList2Integer(gFetchMap, index + 2);
        if ((now - timestamp) < HTTP_REQUEST_TIMEOUT_SEC)
        {
            kept += llList2List(gFetchMap, index, index + 2);
        }
        else
        {
            key avatar = llList2Key(gFetchMap, index + 1);
            debugTrace("Pruning stale fetch for avatar=" + (string)avatar);
            integer inflightIndex = llListFindList(gInFlightAvatars, [avatar]);
            if (inflightIndex != -1)
            {
                gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
            }
        }
    }
    gFetchMap = kept;
}

pruneAllStale(integer now)
{
    if ((now - gLastPruneTime) < PRUNE_INTERVAL_SEC)
    {
        return;
    }
    gLastPruneTime = now;
    pruneOldGreets(now);
    pruneStaleRequests(now);
    pruneStaleFetches(now);
}

addFetchMap(key req, key avatar)
{
    integer entries = llGetListLength(gFetchMap) / 3;
    if (entries >= MAX_FETCH_MAP)
    {
        gFetchMap = llDeleteSubList(gFetchMap, 0, 2);
    }
    gFetchMap += [req, avatar, nowUnix()];
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
    gPendingAvatars = [];
    gPendingStartTimes = [];
    gPendingLastNotice = [];
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

integer findPendingIndex(key avatar)
{
    return llListFindList(gPendingAvatars, [avatar]);
}

markPending(key avatar, integer now)
{
    integer index = findPendingIndex(avatar);
    if (index == -1)
    {
        gPendingAvatars += [avatar];
        gPendingStartTimes += [now];
        gPendingLastNotice += [0];
        return;
    }
    gPendingStartTimes = llListReplaceList(gPendingStartTimes, [now], index, index);
    gPendingLastNotice = llListReplaceList(gPendingLastNotice, [0], index, index);
}

clearPending(key avatar)
{
    integer index = findPendingIndex(avatar);
    if (index == -1)
    {
        return;
    }
    gPendingAvatars = llDeleteSubList(gPendingAvatars, index, index);
    gPendingStartTimes = llDeleteSubList(gPendingStartTimes, index, index);
    gPendingLastNotice = llDeleteSubList(gPendingLastNotice, index, index);
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

retryQueuedAfterCallbackRefresh()
{
    integer count = llGetListLength(gActiveAvatars);
    integer index;
    for (index = 0; index < count; ++index)
    {
        key avatar = llList2Key(gActiveAvatars, index);
        if (!isActive(avatar))
        {
            jump continue_retry;
        }
        if (isInFlight(avatar))
        {
            jump continue_retry;
        }
        string queued = getQueuedMessage(avatar);
        if (queued != "")
        {
            clearQueuedMessage(avatar);
            sendMessage(avatar, queued);
        }
@continue_retry;
    }
}

handlePackageResult(key avatar, string chat, string act)
{
    if (avatar == NULL_KEY)
    {
        return;
    }
    clearPending(avatar);
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
        sayTo(avatar, chat);
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

handlePipePackage(string body)
{
    if (!ensureMemory("handlePipePackage"))
    {
        return;
    }
    string msgType = pipeGetValue(body, "TYPE");
    if (msgType != "PKG")
    {
        return;
    }
    key avatar = (key)pipeGetValue(body, "USER");
    string chat = pipeGetValue(body, "CHAT");
    string act = pipeGetValue(body, "ACT");
    handlePackageResult(avatar, chat, act);
}

string buildPayload(key avatar, string message, string clientReqId)
{
    return llList2Json(JSON_OBJECT, [
        "npc_id", getNpcId(),
        "avatar_key", (string)avatar,
        "avatar_name", llGetUsername(avatar),
        "avatar_display_name", llGetDisplayName(avatar),
        "avatar_username", llGetUsername(avatar),
        "object_key", (string)getRootKey(),
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
    if (!ensureMemory("sendMessage"))
    {
        sayTo(avatar, "Sorry, I'm restarting due to low memory. Try again in a moment.");
        return;
    }
    if (!ensureRequestCapacity())
    {
        sayTo(avatar, "Sorry, I'm busy. Try again in a moment.");
        return;
    }
    string clientReqId = (string)llGenerateKey();
    string payload = buildPayload(avatar, message, clientReqId);
    string serverBase = getServerBase();
    debugTrace("sendMessage avatar=" + (string)avatar + " server=" + serverBase + " cb=" + (string)(gCallbackToken != ""));
    if (serverBase == "")
    {
        llOwnerSay("SERVER_BASE not set. Add SERVER_BASE=<url> to the object description.");
        sayTo(avatar, "NPC is offline. Server URL is not configured.");
        return;
    }
    string url = serverBase + "/chat";
    integer isAsync = FALSE;
    if (gCallbackToken != "")
    {
        url = serverBase + "/chat_async";
        isAsync = TRUE;
    }
    else if (ASYNC_ONLY)
    {
        llMessageLinked(LINK_SET, LM_CB_REFRESH, "", NULL_KEY);
        sayTo(avatar, "Please wait, initializing callback.");
        return;
    }
    debugTrace("request url=" + url + " async=" + (string)isAsync);
    gInFlightAvatars += [avatar];
    if (isAsync)
    {
        markPending(avatar, nowUnix());
    }
    key requestId = llHTTPRequest(url, [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"], payload);
    gRequestMap += [requestId, avatar, isAsync, message, serverBase, 0, nowUnix()];
}

integer isRetriableStatus(integer status, string body)
{
    if (status == 502 || status == 503 || status == 504)
    {
        return TRUE;
    }
    if (llSubStringIndex(body, "Unknown Host") != -1)
    {
        return TRUE;
    }
    return FALSE;
}

integer retryWithFallback(key avatar, string message, integer isAsync, integer attempt)
{
    if (attempt > 0)
    {
        return FALSE;
    }
    string fallbackBase = getServerBaseFallback();
    if (fallbackBase == "")
    {
        return FALSE;
    }
    string url = fallbackBase + "/chat";
    if (isAsync)
    {
        url = fallbackBase + "/chat_async";
    }
    debugTrace("retrying with fallback server=" + fallbackBase + " async=" + (string)isAsync);
    string clientReqId = (string)llGenerateKey();
    string payload = buildPayload(avatar, message, clientReqId);
    key requestId = llHTTPRequest(url, [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"], payload);
    gRequestMap += [requestId, avatar, isAsync, message, fallbackBase, attempt + 1, nowUnix()];
    return TRUE;
}

integer retryWithPrimary(string serverBase, key avatar, string message, integer isAsync, integer attempt)
{
    if (attempt > 0)
    {
        return FALSE;
    }
    if (serverBase == "")
    {
        return FALSE;
    }
    string url = serverBase + "/chat";
    if (isAsync)
    {
        url = serverBase + "/chat_async";
    }
    debugTrace("retrying with primary server=" + serverBase + " async=" + (string)isAsync);
    string clientReqId = (string)llGenerateKey();
    string payload = buildPayload(avatar, message, clientReqId);
    key requestId = llHTTPRequest(url, [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"], payload);
    gRequestMap += [requestId, avatar, isAsync, message, serverBase, attempt + 1, nowUnix()];
    return TRUE;
}

notifyFallbackHint(integer status, string body)
{
    if (!isRetriableStatus(status, body))
    {
        return;
    }
    if (getServerBaseFallback() != "")
    {
        return;
    }
    llOwnerSay("Server hostname unresolved. Set SERVER_BASE_FALLBACK=<url> (IP or alternate domain) in the object description.");
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
    sayTo(avatar, "Hello " + llGetDisplayName(avatar) + "! Say something in public chat near me.");
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
    sayTo(avatar, message);
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
        if (!ensureMemory("touch_start"))
        {
            sayTo(toucher, "Sorry, I'm restarting due to low memory. Try again in a moment.");
            return;
        }
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
        if (!ensureMemory("http_response"))
        {
            return;
        }
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
            gFetchMap = llDeleteSubList(gFetchMap, fetchIndex, fetchIndex + 2);
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
                    sayTo(avatar, "Sorry, fetch failed. Try again.");
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
        string message = llList2String(gRequestMap, requestIndex + 3);
        string serverBase = llList2String(gRequestMap, requestIndex + 4);
        integer attempt = llList2Integer(gRequestMap, requestIndex + 5);
        debugTrace("response status=" + (string)status + " async=" + (string)isAsync);
        if (isAsync)
        {
            debugTrace("sync response status=" + (string)status + " body=" + (string)llStringLength(body));
        }
        gRequestMap = llDeleteSubList(gRequestMap, requestIndex, requestIndex + 6);

        if (status != 200 && isRetriableStatus(status, body))
        {
            if (retryWithFallback(activeAvatar, message, isAsync, attempt))
            {
                return;
            }
            if (retryWithPrimary(serverBase, activeAvatar, message, isAsync, attempt))
            {
                return;
            }
            debugTrace("fallback unavailable or already used for server=" + serverBase);
            notifyFallbackHint(status, body);
        }
        if (!isActive(activeAvatar))
        {
            return;
        }

        if (isAsync)
        {
            if (status != 200)
            {
                if (status == 409 && llGetSubString(body, 0, 0) == "{")
                {
                    string errorCode = llJsonGetValue(body, ["error"]);
                    if (errorCode == "callback_not_registered")
                    {
                        if (message != "")
                        {
                            setQueuedMessage(activeAvatar, message);
                        }
                        gCallbackToken = "";
                        llMessageLinked(LINK_SET, LM_CB_REFRESH, "", NULL_KEY);
                        if (isActive(activeAvatar))
                        {
                            sayTo(activeAvatar, "Reconnecting... Please wait a moment.");
                        }
                        integer inflightIndex = llListFindList(gInFlightAvatars, [activeAvatar]);
                        if (inflightIndex != -1)
                        {
                            gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
                        }
                        clearPending(activeAvatar);
                        updateDebugTexture(activeAvatar);
                        return;
                    }
                }
                integer inflightIndex = llListFindList(gInFlightAvatars, [activeAvatar]);
                if (inflightIndex != -1)
                {
                    gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
                }
                clearPending(activeAvatar);
                updateDebugTexture(activeAvatar);
                sayTo(activeAvatar, "ERROR Status: " + (string) status + ": Sorry, I glitched. Try again.");
                string queued = getQueuedMessage(activeAvatar);
                if (queued != "")
                {
                    clearQueuedMessage(activeAvatar);
                    sendMessage(activeAvatar, queued);
                }
            }
            return;
        }

        string replyType = llJsonValueType(body, ["reply"]);
        string reply = "";
        if (replyType == JSON_STRING)
        {
            reply = llJsonGetValue(body, ["reply"]);
        }

        if (DEBUG)
        {
            integer bodyLength = llStringLength(body);
            if (bodyLength > 300)
            {
                llOwnerSay(llGetSubString(body, 0, 299) + "...");
            }
            else
            {
                llOwnerSay(body);
            }
        }

        integer inflightIndex = llListFindList(gInFlightAvatars, [activeAvatar]);
        if (inflightIndex != -1)
        {
            gInFlightAvatars = llDeleteSubList(gInFlightAvatars, inflightIndex, inflightIndex);
        }

        if (status != 200)
        {
            updateDebugTexture(activeAvatar);
            sayTo(activeAvatar, "ERROR Status: " + (string) status + ": Sorry, I glitched. Try again.");
        }
        else
        {
            if (replyType == JSON_INVALID)
            {
                string okValue = llJsonGetValue(body, ["ok"]);
                if (okValue == JSON_INVALID)
                {
                    updateDebugTexture(activeAvatar);
                    sayTo(activeAvatar, "ERROR Invalid JSON response: Sorry, I glitched. Try again.");
                }
                else
                {
                    updateDebugTexture(activeAvatar);
                    sayTo(activeAvatar, "ERROR Missing reply in server response: Sorry, I glitched. Try again.");
                }
            }
            else
            {
                updateDebugTexture(activeAvatar);
                sayTo(activeAvatar, reply);
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
            retryQueuedAfterCallbackRefresh();
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
            string act = llJsonGetValue(str, ["act"]);
            handlePackageResult(avatar, reply, act);
            return;
        }
        string msgType = pipeGetValue(str, "TYPE");
        if (msgType == "FETCH")
        {
            key avatar = (key)pipeGetValue(str, "USER");
            string token = pipeGetValue(str, "TOKEN");
            if (avatar != NULL_KEY && token != "")
            {
                string url = getServerBase() + "/sl/fetch?token=" + llEscapeURL(token);
                key req = llHTTPRequest(url, [HTTP_METHOD, "GET", HTTP_BODY_MAXLENGTH, FETCH_MAX], "");
                addFetchMap(req, avatar);
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
        pruneAllStale(now);
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
        integer pendingCount = llGetListLength(gPendingAvatars);
        for (index = pendingCount - 1; index >= 0; --index)
        {
            key avatar = llList2Key(gPendingAvatars, index);
            if (!isActive(avatar))
            {
                clearPending(avatar);
                jump continue_pending;
            }
            integer startTime = llList2Integer(gPendingStartTimes, index);
            integer lastNotice = llList2Integer(gPendingLastNotice, index);
            if (startTime > 0 && (now - startTime) >= ASYNC_WAIT_NOTICE_SEC && (now - lastNotice) >= ASYNC_WAIT_COOLDOWN_SEC)
            {
                sayTo(avatar, "Please wait, I'm thinking...");
                gPendingLastNotice = llListReplaceList(gPendingLastNotice, [now], index, index);
            }
@continue_pending;
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
