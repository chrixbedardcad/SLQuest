// SLQuest_QuestObject.lsl - Generic quest object script
// Reads config from quest_config notecard, registers with shared pool, sends events on touch

string SERVER_BASE = "http://slquest.duckdns.org:8001";
string NOTECARD_NAME = "quest_config";
float HEARTBEAT_INTERVAL = 300.0;  // 5 minutes

// Config values (from notecard)
string g_object_id = "";
integer g_difficulty = 1;
string g_hint = "";
string g_found_message = "You found it!";
string g_category = "hidden";

// State
key g_notecard_query = NULL_KEY;
integer g_notecard_line = 0;
list g_notecard_lines = [];

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

parseNotecardLine(string line)
{
    line = llStringTrim(line, STRING_TRIM);
    if (line == "" || llGetSubString(line, 0, 0) == "#")
    {
        return;
    }
    integer eq_pos = llSubStringIndex(line, "=");
    if (eq_pos < 1)
    {
        return;
    }
    string key = llStringTrim(llGetSubString(line, 0, eq_pos - 1), STRING_TRIM);
    string value = llStringTrim(llGetSubString(line, eq_pos + 1, -1), STRING_TRIM);

    if (key == "object_id") g_object_id = value;
    else if (key == "difficulty") g_difficulty = (integer)value;
    else if (key == "hint") g_hint = value;
    else if (key == "found_message") g_found_message = value;
    else if (key == "category") g_category = value;
}

parseAllNotecardLines()
{
    integer i;
    for (i = 0; i < llGetListLength(g_notecard_lines); ++i)
    {
        parseNotecardLine(llList2String(g_notecard_lines, i));
    }
    g_notecard_lines = [];
}

sendRegistration()
{
    if (g_object_id == "")
    {
        llOwnerSay("QuestObject: No object_id configured, cannot register.");
        return;
    }

    vector pos = llGetPos();
    string pos_str = "<" + (string)((integer)pos.x) + "," +
                          (string)((integer)pos.y) + "," +
                          (string)((integer)pos.z) + ">";

    string payload = llList2Json(JSON_OBJECT, [
        "object_id", g_object_id,
        "object_key", (string)llGetKey(),
        "object_name", llGetObjectName(),
        "region", llGetRegionName(),
        "position", pos_str,
        "difficulty", g_difficulty,
        "hint", g_hint,
        "found_message", g_found_message,
        "category", g_category
    ]);

    llHTTPRequest(
        getServerBase() + "/pool/register",
        [
            HTTP_METHOD, "POST",
            HTTP_MIMETYPE, "application/json"
        ],
        payload
    );
}

sendObjectFound(key avatar)
{
    if (g_object_id == "")
    {
        return;
    }

    string payload = llList2Json(JSON_OBJECT, [
        "avatar_key", (string)avatar,
        "object_id", g_object_id,
        "object_key", (string)llGetKey(),
        "event", "object_found",
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

    if (g_found_message != "")
    {
        llRegionSayTo(avatar, 0, g_found_message);
    }
}

startNotecardRead()
{
    if (llGetInventoryType(NOTECARD_NAME) != INVENTORY_NOTECARD)
    {
        llOwnerSay("QuestObject: No " + NOTECARD_NAME + " notecard found. Using object description for config.");
        // Try to get object_id from description as fallback
        g_object_id = getConfigValue("object_id", "");
        g_difficulty = (integer)getConfigValue("difficulty", "1");
        g_hint = getConfigValue("hint", "");
        g_found_message = getConfigValue("found_message", "You found it!");
        g_category = getConfigValue("category", "hidden");

        if (g_object_id != "")
        {
            llOwnerSay("QuestObject: Configured as '" + g_object_id + "', registering...");
            sendRegistration();
            llSetTimerEvent(HEARTBEAT_INTERVAL);
        }
        return;
    }

    g_notecard_line = 0;
    g_notecard_lines = [];
    g_notecard_query = llGetNotecardLine(NOTECARD_NAME, g_notecard_line);
}

default
{
    state_entry()
    {
        startNotecardRead();
    }

    on_rez(integer start_param)
    {
        startNotecardRead();
    }

    changed(integer change)
    {
        if (change & CHANGED_REGION_START)
        {
            // Sim restart - re-register
            if (g_object_id != "")
            {
                sendRegistration();
            }
        }
        if (change & CHANGED_INVENTORY)
        {
            // Notecard might have changed - re-read
            startNotecardRead();
        }
    }

    dataserver(key query_id, string data)
    {
        if (query_id != g_notecard_query)
        {
            return;
        }

        if (data == EOF)
        {
            // Finished reading notecard
            parseAllNotecardLines();

            if (g_object_id == "")
            {
                llOwnerSay("QuestObject: Warning - no object_id in notecard config.");
                return;
            }

            llOwnerSay("QuestObject: Configured as '" + g_object_id + "' (difficulty=" + (string)g_difficulty + "), registering...");
            sendRegistration();
            llSetTimerEvent(HEARTBEAT_INTERVAL);
            return;
        }

        g_notecard_lines += [data];
        g_notecard_line++;
        g_notecard_query = llGetNotecardLine(NOTECARD_NAME, g_notecard_line);
    }

    timer()
    {
        // Heartbeat - re-register to confirm alive
        sendRegistration();
    }

    touch_start(integer total_number)
    {
        key toucher = llDetectedKey(0);
        if (toucher == NULL_KEY)
        {
            return;
        }
        sendObjectFound(toucher);
    }

    http_response(key request_id, integer status, list metadata, string body)
    {
        if (status == 200)
        {
            // Registration successful - silent
        }
        else
        {
            llOwnerSay("QuestObject: Server returned status " + (string)status);
        }
    }
}
