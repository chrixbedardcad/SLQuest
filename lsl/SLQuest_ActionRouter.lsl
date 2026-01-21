integer LM_ACTION = 9200;
list GIVE_ALLOW = ["Blue Feather"];

list splitActions(string body)
{
    return llParseString2List(body, [";"], []);
}

float parseVolume(string args)
{
    list parts = llParseString2List(args, [","], []);
    integer i;
    for (i = 0; i < llGetListLength(parts); ++i)
    {
        string part = llStringTrim(llList2String(parts, i), STRING_TRIM);
        if (llSubStringIndex(part, "vol=") == 0)
        {
            string raw = llGetSubString(part, 4, -1);
            float vol = (float)raw;
            if (vol < 0.0)
            {
                return 0.0;
            }
            if (vol > 1.0)
            {
                return 1.0;
            }
            return vol;
        }
    }
    return 1.0;
}

string parseFirstToken(string args)
{
    list parts = llParseString2List(args, [","], []);
    if (llGetListLength(parts) == 0)
    {
        return "";
    }
    return llStringTrim(llList2String(parts, 0), STRING_TRIM);
}

integer isAllowedGive(string item)
{
    return llListFindList(GIVE_ALLOW, [item]) != -1;
}

applyParticlePreset(string preset)
{
    if (preset == "Sparkle")
    {
        llParticleSystem([
            PSYS_PART_FLAGS, PSYS_PART_EMISSIVE_MASK | PSYS_PART_INTERP_COLOR_MASK,
            PSYS_SRC_PATTERN, PSYS_SRC_PATTERN_DROP,
            PSYS_PART_START_COLOR, <0.6, 0.9, 1.0>,
            PSYS_PART_END_COLOR, <0.3, 0.6, 1.0>,
            PSYS_PART_START_ALPHA, 0.8,
            PSYS_PART_END_ALPHA, 0.0,
            PSYS_PART_START_SCALE, <0.1, 0.1, 0.0>,
            PSYS_PART_END_SCALE, <0.2, 0.2, 0.0>,
            PSYS_PART_MAX_AGE, 1.0,
            PSYS_SRC_MAX_AGE, 2.0,
            PSYS_SRC_BURST_RATE, 0.05,
            PSYS_SRC_BURST_PART_COUNT, 2,
            PSYS_SRC_BURST_RADIUS, 0.0,
            PSYS_SRC_BURST_SPEED_MIN, 0.1,
            PSYS_SRC_BURST_SPEED_MAX, 0.4
        ]);
        return;
    }
    if (preset == "Clear")
    {
        llParticleSystem([]);
    }
}

default
{
    link_message(integer sender, integer num, string str, key id)
    {
        if (num != LM_ACTION)
        {
            return;
        }
        list actions = splitActions(str);
        integer i;
        for (i = 0; i < llGetListLength(actions); ++i)
        {
            string action = llStringTrim(llList2String(actions, i), STRING_TRIM);
            if (action == "")
            {
                jump continue_action;
            }
            integer colon = llSubStringIndex(action, ":");
            if (colon <= 0)
            {
                jump continue_action;
            }
            string kind = llGetSubString(action, 0, colon - 1);
            string args = llGetSubString(action, colon + 1, -1);
            if (kind == "Sound")
            {
                string soundToken = parseFirstToken(args);
                float vol = parseVolume(args);
                key soundKey = (key)soundToken;
                if (soundKey != NULL_KEY)
                {
                    llPlaySound(soundKey, vol);
                }
                else if (soundToken != "")
                {
                    llTriggerSound(soundToken, vol);
                }
            }
            else if (kind == "AnimStart")
            {
                string anim = llStringTrim(args, STRING_TRIM);
                if (anim != "")
                {
                    llStartObjectAnimation(anim);
                }
            }
            else if (kind == "AnimStop")
            {
                string anim = llStringTrim(args, STRING_TRIM);
                if (anim != "")
                {
                    llStopObjectAnimation(anim);
                }
            }
            else if (kind == "Particle")
            {
                string preset = llStringTrim(args, STRING_TRIM);
                if (preset != "")
                {
                    applyParticlePreset(preset);
                }
            }
            else if (kind == "Give")
            {
                string item = llStringTrim(args, STRING_TRIM);
                if (id != NULL_KEY && item != "" && isAllowedGive(item))
                {
                    if (llGetInventoryType(item) != INVENTORY_NONE)
                    {
                        llGiveInventory(id, item);
                    }
                }
            }
@continue_action;
        }
    }
}
