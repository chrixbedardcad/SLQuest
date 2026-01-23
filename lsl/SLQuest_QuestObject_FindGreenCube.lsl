string SERVER_BASE = "http://slquest.duckdns.org:8001";
string QUEST_ID = "find_green_cube";
string EVENT_NAME = "cube_clicked";

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

default
{
    touch_start(integer total_number)
    {
        key toucher = llDetectedKey(0);
        if (toucher == NULL_KEY)
        {
            return;
        }
        string payload = llList2Json(JSON_OBJECT, [
            "avatar_key", (string)toucher,
            "quest_id", QUEST_ID,
            "event", EVENT_NAME,
            "object_key", (string)llGetKey(),
            "ts", llGetTimestamp()
        ]);
        llHTTPRequest(
            getServerBase() + "/quest/event",
            [
                HTTP_METHOD, "POST",
                HTTP_MIMETYPE, "application/json"
            ],
            payload
        );
    }
}
