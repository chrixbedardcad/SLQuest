// SL Quest Gateway Client
// How to use:
// 1) Drop this script into an object you own.
// 2) Set TOKEN to match the server's SL_TOKEN.
// 3) Touch the object to start, then speak on channel 7 to send messages.

string BASE_URL = "http://slquest.duckdns.org/sl";
string TOKEN = "CHANGE_ME";
integer LISTEN_CHANNEL = 7;

string session_id;
key request_id;

sendRequest(string message)
{
    string body = llList2Json(JSON_OBJECT, [
        "avatar_name", llKey2Name(llGetOwner()),
        "avatar_key", (string)llGetOwner(),
        "object_key", (string)llGetKey(),
        "region", llGetRegionName(),
        "message", message,
        "session_id", session_id
    ]);

    string url = BASE_URL + "?token=" + llEscapeURL(TOKEN);

    request_id = llHTTPRequest(
        url,
        [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"],
        body
    );
}

default
{
    state_entry()
    {
        session_id = (string)llGetOwner() + ":" + (string)llGetKey();
        llListen(LISTEN_CHANNEL, "", llGetOwner(), "");
        llOwnerSay("SL Quest client ready. Touch to start, say on channel 7 to reply.");
    }

    touch_start(integer total_number)
    {
        sendRequest("start");
    }

    listen(integer channel, string name, key id, string message)
    {
        sendRequest(message);
    }

    http_response(key req, integer status, list metadata, string body)
    {
        if (req != request_id)
        {
            return;
        }

        if (status != 200)
        {
            llOwnerSay("HTTP error " + (string)status + ": " + body);
            return;
        }

        if (llJsonValueType(body, ["reply"]) == JSON_STRING)
        {
            string reply = llJsonGetValue(body, ["reply"]);
            llSay(0, reply);
        }
        else
        {
            string truncated = body;
            if (llStringLength(truncated) > 200)
            {
                truncated = llGetSubString(truncated, 0, 199) + "...";
            }
            llSay(0, truncated);
        }
    }
}
