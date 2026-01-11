// How to use:
// 1) Drop this script into an in-world object.
// 2) Set BASE_URL and TOKEN as needed.
// 3) Touch the object to start, or chat on channel 7 as the owner.

string BASE_URL = "http://slquest.duckdns.org/slquest";
string TOKEN = "";

string session_id()
{
    return (string)llGetOwner() + ":" + (string)llGetKey();
}

string buildUrl()
{
    if (TOKEN != "")
    {
        return BASE_URL + "?token=" + llEscapeURL(TOKEN);
    }
    return BASE_URL;
}

string buildPayload(string message)
{
    return llList2Json(JSON_OBJECT, [
        "avatar_name", llKey2Name(llGetOwner()),
        "avatar_key", (string)llGetOwner(),
        "object_name", llGetObjectName(),
        "object_key", (string)llGetKey(),
        "region", llGetRegionName(),
        "message", message,
        "session_id", session_id()
    ]);
}

sendMessage(string message)
{
    string payload = buildPayload(message);
    llHTTPRequest(buildUrl(), [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"], payload);
}

default
{
    state_entry()
    {
        llListen(7, "", llGetOwner(), "");
    }

    touch_start(integer total_number)
    {
        sendMessage("start");
    }

    listen(integer channel, string name, key id, string message)
    {
        if (id == llGetOwner())
        {
            sendMessage(message);
        }
    }

    http_response(key request_id, integer status, list metadata, string body)
    {
        if (status != 200)
        {
            llOwnerSay("HTTP error " + (string)status + " body=" + body);
            return;
        }

        string reply = llJsonGetValue(body, ["reply"]);
        if (reply == JSON_INVALID)
        {
            integer maxLen = 200;
            if (llStringLength(body) > maxLen)
            {
                body = llGetSubString(body, 0, maxLen - 1);
            }
            llOwnerSay("Invalid JSON: " + body);
            return;
        }

        llSay(0, reply);
    }
}
