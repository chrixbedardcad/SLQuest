string SERVER_BASE = "http://slquest.duckdns.org:8001";
string NPC_ID = "";
integer LM_CB_TOKEN = 9100;
integer LM_CB_REPLY = 9101;

string gCallbackURL = "";
string gCallbackToken = "";
key gRegisterReq = NULL_KEY;

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

registerCallback()
{
    string payload = llList2Json(JSON_OBJECT, [
        "object_key", (string)llGetKey(),
        "npc_id", getNpcId(),
        "region", llGetRegionName(),
        "callback_url", gCallbackURL,
        "ts", llGetTimestamp()
    ]);
    gRegisterReq = llHTTPRequest(
        SERVER_BASE + "/sl/callback/register",
        [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"],
        payload
    );
}

default
{
    state_entry()
    {
        // Must run in the same linkset as SLQuest_ChatClient.lsl.
        llRequestURL();
    }

    http_request(key id, string method, string body)
    {
        if (method == URL_REQUEST_GRANTED)
        {
            gCallbackURL = body;
            registerCallback();
            return;
        }
        if (method == URL_REQUEST_DENIED)
        {
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
        string qs = llGetHTTPHeader(id, "x-query-string");
        string tokenQ = getQueryParam(qs, "t");
        if (tokenQ != "" && tokenQ != gCallbackToken)
        {
            return;
        }
        if (tokenQ == "")
        {
            string token = llJsonGetValue(body, ["callback_token"]);
            if (token != gCallbackToken)
            {
                return;
            }
        }
        llMessageLinked(LINK_SET, LM_CB_REPLY, body, NULL_KEY);
    }

    http_response(key request_id, integer status, list metadata, string body)
    {
        if (request_id != gRegisterReq)
        {
            return;
        }
        if (status != 200)
        {
            llOwnerSay("Callback registration failed: status=" + (string)status);
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

    on_rez(integer start_param)
    {
        llResetScript();
    }

    changed(integer change)
    {
        if (change & (CHANGED_OWNER | CHANGED_REGION_START))
        {
            llResetScript();
        }
    }
}
