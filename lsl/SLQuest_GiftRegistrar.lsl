// SLQuest_GiftRegistrar.lsl
// Scans inventory for gift items and registers them with the server
// Place this script in the NPC object alongside other SLQuest scripts

string SERVER_BASE = "https://api.slquest.net";
string GIFT_PREFIX = "Gift_";  // Items starting with this prefix are registered as gifts
float REGISTER_INTERVAL = 300.0;  // Re-register every 5 minutes

integer DEBUG = TRUE;

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

string getNpcId()
{
    return getConfigValue("NPC_ID", llGetObjectName());
}

registerGifts()
{
    list gift_names = [];
    integer count = llGetInventoryNumber(INVENTORY_ALL);
    integer i;

    for (i = 0; i < count; ++i)
    {
        string item_name = llGetInventoryName(INVENTORY_ALL, i);
        // Check if item starts with GIFT_PREFIX
        if (llSubStringIndex(item_name, GIFT_PREFIX) == 0)
        {
            gift_names += [item_name];
        }
    }

    if (llGetListLength(gift_names) == 0)
    {
        if (DEBUG) llOwnerSay("GiftRegistrar: No gifts found (items must start with '" + GIFT_PREFIX + "')");
        return;
    }

    string npc_id = getNpcId();
    string gifts_json = llList2Json(JSON_ARRAY, gift_names);

    string payload = llList2Json(JSON_OBJECT, [
        "npc_id", npc_id,
        "npc_key", (string)llGetKey(),
        "region", llGetRegionName(),
        "gifts", gifts_json
    ]);

    if (DEBUG) llOwnerSay("GiftRegistrar: Registering " + (string)llGetListLength(gift_names) + " gifts: " + gifts_json);

    llHTTPRequest(
        getServerBase() + "/pool/gifts/register",
        [
            HTTP_METHOD, "POST",
            HTTP_MIMETYPE, "application/json"
        ],
        payload
    );
}

default
{
    state_entry()
    {
        if (DEBUG) llOwnerSay("GiftRegistrar: Starting, scanning for items with prefix '" + GIFT_PREFIX + "'");
        registerGifts();
        llSetTimerEvent(REGISTER_INTERVAL);
    }

    on_rez(integer start_param)
    {
        registerGifts();
    }

    changed(integer change)
    {
        if (change & CHANGED_INVENTORY)
        {
            // Inventory changed, re-register gifts
            if (DEBUG) llOwnerSay("GiftRegistrar: Inventory changed, re-registering gifts");
            registerGifts();
        }
        if (change & CHANGED_REGION_START)
        {
            registerGifts();
        }
    }

    timer()
    {
        registerGifts();
    }

    http_response(key request_id, integer status, list metadata, string body)
    {
        if (status == 200)
        {
            if (DEBUG) llOwnerSay("GiftRegistrar: Registration successful");
        }
        else
        {
            llOwnerSay("GiftRegistrar: Registration failed, status=" + (string)status);
        }
    }
}
