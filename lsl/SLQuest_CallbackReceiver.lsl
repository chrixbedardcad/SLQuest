string SERVER_BASE = "http://slquest.duckdns.org:8001";
string SERVER_BASE_FALLBACK = "";
string NPC_ID = "";
integer LM_CB_TOKEN = 9100;
integer LM_CB_REPLY = 9101;
integer DEBUG = TRUE;
string DEBUG_TAG = "SLQuest Debug: ";

// Greeting configuration
integer GREET_ENABLED = FALSE;
float GREET_RANGE = 12.0;
float GREET_INTERVAL = 10.0;
integer GREET_COOLDOWN_PER_AVATAR_SEC = 300;
integer GREET_GLOBAL_COOLDOWN_SEC = 20;
integer GREET_SKIP_OWNER = TRUE;
integer GREET_PRUNE_INTERVAL_SEC = 60;

string gCallbackURL = "";
string gCallbackToken = "";
key gRegisterReq = NULL_KEY;
integer LM_CB_REFRESH = 9102;
integer gRequestInFlight = FALSE;

// Greeting state
integer gNextGlobalGreetAt = 0;
list gGreetMemory = [];
integer gLastGreetPruneTime = 0;

debugTrace(string message)
{
    if (!DEBUG)
    {
        return;
    }
    llOwnerSay(DEBUG_TAG + message);
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
    llOwnerSay("Callback register failed to resolve hostname. Set SERVER_BASE_FALLBACK=<url> (IP or alternate domain) in the object description.");
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

string getQueryParam(string qs, string ikey)
{
    list parts = llParseString2List(qs, ["&"], []);
    integer i;
    for (i = 0; i < llGetListLength(parts); ++i)
    {
        string part = llList2String(parts, i);
        integer eq = llSubStringIndex(part, "=");
        if (eq > 0)
        {
            string k = llGetSubString(part, 0, eq - 1);
            if (k == ikey)
            {
                return llUnescapeURL(llGetSubString(part, eq + 1, -1));
            }
        }
    }
    return "";
}

string getQueryString(key reqId)
{
    string qs = llGetHTTPHeader(reqId, "x-query-string");
    if (qs != "")
    {
        return qs;
    }
    qs = llGetHTTPHeader(reqId, "x-querystring");
    if (qs != "")
    {
        return qs;
    }
    return "";
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

string extractCallbackTokenFromBody(string body)
{
    if (llSubStringIndex(body, "CB=") == -1)
    {
        return "";
    }
    return pipeGetValue(body, "CB");
}

integer nowUnix()
{
    return llGetUnixTime();
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
    if ((now - gLastGreetPruneTime) < GREET_PRUNE_INTERVAL_SEC)
    {
        return;
    }
    gLastGreetPruneTime = now;
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

registerCallback()
{
    string serverBase = getServerBase();
    if (serverBase == "")
    {
        llOwnerSay("SERVER_BASE not set. Add SERVER_BASE=<url> to the object description.");
        return;
    }
    string payload = llList2Json(JSON_OBJECT, [
        "object_key", (string)getRootKey(),
        "npc_id", getNpcId(),
        "region", llGetRegionName(),
        "callback_url", gCallbackURL,
        "ts", llGetTimestamp()
    ]);
    gRegisterReq = llHTTPRequest(
        serverBase + "/sl/callback/register",
        [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"],
        payload
    );
    debugTrace("register callback url=" + gCallbackURL);
}

requestCallbackURL()
{
    if (gCallbackURL != "")
    {
        llReleaseURL(gCallbackURL);
    }
    gCallbackURL = "";
    gCallbackToken = "";
    gRegisterReq = NULL_KEY;
    gRequestInFlight = TRUE;
    llRequestURL();
}

default
{
    state_entry()
    {
        // Must run in the same linkset as SLQuest_ChatClient.lsl.
        debugTrace("state_entry server=" + getServerBase());
        if (getServerBase() == "")
        {
            llOwnerSay("SERVER_BASE not set. Add SERVER_BASE=<url> to the object description.");
            return;
        }
        requestCallbackURL();
        if (GREET_ENABLED)
        {
            llSensorRepeat("", NULL_KEY, AGENT, GREET_RANGE, PI, GREET_INTERVAL);
            llSetTimerEvent(GREET_PRUNE_INTERVAL_SEC);
        }
    }

    http_request(key id, string method, string body)
    {
        if (method == URL_REQUEST_GRANTED)
        {
            gRequestInFlight = FALSE;
            gCallbackURL = body;
            debugTrace("callback url granted");
            registerCallback();
            return;
        }
        if (method == URL_REQUEST_DENIED)
        {
            gRequestInFlight = FALSE;
            llOwnerSay("Callback URL request denied: " + body);
            return;
        }
        if (method != "POST")
        {
            llHTTPResponse(id, 405, "method_not_allowed");
            return;
        }
        llHTTPResponse(id, 200, "ok");
        if (gCallbackToken == "")
        {
            return;
        }
        string qs = getQueryString(id);
        string tokenQ = getQueryParam(qs, "t");
        if (tokenQ == "")
        {
            tokenQ = extractCallbackTokenFromBody(body);
        }
        if (tokenQ != "" && tokenQ != gCallbackToken)
        {
            debugTrace("callback token mismatch");
            return;
        }
        if (tokenQ == "")
        {
            string token = llJsonGetValue(body, ["callback_token"]);
            if (token != gCallbackToken)
            {
                debugTrace("callback token missing/invalid qs=" + qs);
                return;
            }
        }
        string msgType = pipeGetValue(body, "TYPE");
        if (msgType == "PKG")
        {
            key avatar = (key)pipeGetValue(body, "USER");
            string reply = pipeGetValue(body, "CHAT");
            string act = pipeGetValue(body, "ACT");
            string small = llList2Json(JSON_OBJECT, [
                "avatar_key", (string)avatar,
                "reply", reply,
                "act", act
            ]);
            llMessageLinked(LINK_SET, LM_CB_REPLY, small, NULL_KEY);
            return;
        }
        llMessageLinked(LINK_SET, LM_CB_REPLY, body, NULL_KEY);
    }

    http_response(key request_id, integer status, list metadata, string body)
    {
        if (request_id != gRegisterReq)
        {
            return;
        }
        debugTrace("register response status=" + (string)status);
        if (status != 200)
        {
            if (isRetriableStatus(status, body) && getServerBaseFallback() != "")
            {
                string fallbackBase = getServerBaseFallback();
                string payload = llList2Json(JSON_OBJECT, [
                    "object_key", (string)getRootKey(),
                    "npc_id", getNpcId(),
                    "region", llGetRegionName(),
                    "callback_url", gCallbackURL,
                    "ts", llGetTimestamp()
                ]);
                gRegisterReq = llHTTPRequest(
                    fallbackBase + "/sl/callback/register",
                    [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"],
                    payload
                );
                debugTrace("register callback fallback server=" + fallbackBase);
                return;
            }
            llOwnerSay("Callback registration failed: status=" + (string)status);
            notifyFallbackHint(status, body);
            return;
        }
        string token = llJsonGetValue(body, ["callback_token"]);
        if (token == JSON_INVALID || token == "")
        {
            llOwnerSay("Callback registration missing token.");
            return;
        }
        gCallbackToken = token;
        llMessageLinked(LINK_SET, LM_CB_TOKEN, gCallbackToken, NULL_KEY);
    }

    link_message(integer sender, integer num, string str, key id)
    {
        if (num != LM_CB_REFRESH)
        {
            return;
        }
        if (gCallbackURL == "" && !gRequestInFlight)
        {
            requestCallbackURL();
            return;
        }
        registerCallback();
    }

    state_exit()
    {
        if (gCallbackURL != "")
        {
            llReleaseURL(gCallbackURL);
        }
    }

    on_rez(integer start_param)
    {
        llResetScript();
    }

    timer()
    {
        if (GREET_ENABLED)
        {
            pruneOldGreets(nowUnix());
        }
    }

    sensor(integer total_number)
    {
        if (!GREET_ENABLED)
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
        if (change & (CHANGED_OWNER | CHANGED_REGION_START))
        {
            if (gCallbackURL != "")
            {
                llReleaseURL(gCallbackURL);
            }
            llResetScript();
        }
    }
}
