obs = obslua

local DEFAULT_CYCLE_SECONDS = 171900 -- 47 hours 45 minutes
local DEFAULT_RESTART_DELAY_SECONDS = 10

local cycle_seconds = DEFAULT_CYCLE_SECONDS
local restart_delay_seconds = DEFAULT_RESTART_DELAY_SECONDS
local stream_started_at = nil
local restart_at = nil
local cycle_stop_pending = false

local function log_info(message)
    obs.script_log(obs.LOG_INFO, "[Twitch247 Stream Cycle] " .. message)
end

local function reset_cycle_from_now()
    stream_started_at = os.time()
    restart_at = nil
    cycle_stop_pending = false
    log_info(string.format(
        "Stream timer started; the next planned restart is in %d seconds.",
        cycle_seconds
    ))
end

local function on_frontend_event(event)
    if event == obs.OBS_FRONTEND_EVENT_STREAMING_STARTED then
        reset_cycle_from_now()
        return
    end

    if event == obs.OBS_FRONTEND_EVENT_STREAMING_STOPPED then
        stream_started_at = nil

        if cycle_stop_pending then
            cycle_stop_pending = false
            restart_at = os.time() + restart_delay_seconds
            log_info(string.format(
                "Planned stop completed; restarting the stream in %d seconds.",
                restart_delay_seconds
            ))
        else
            restart_at = nil
            log_info("Stream stopped manually or externally; no automatic start scheduled.")
        end
    end
end

local function on_timer()
    local now = os.time()

    if restart_at ~= nil then
        if now >= restart_at and not obs.obs_frontend_streaming_active() then
            restart_at = nil
            log_info("Starting the stream again after the planned cycle.")
            obs.obs_frontend_streaming_start()
        end
        return
    end

    if stream_started_at == nil then
        if obs.obs_frontend_streaming_active() then
            reset_cycle_from_now()
        end
        return
    end

    if not cycle_stop_pending
        and obs.obs_frontend_streaming_active()
        and now - stream_started_at >= cycle_seconds then
        cycle_stop_pending = true
        log_info("Cycle interval reached; stopping the stream cleanly.")
        obs.obs_frontend_streaming_stop()
    end
end

function script_description()
    return [[
Restarts an active Twitch stream on a fixed cycle so a 24/7 broadcast
does not reach Twitch's 48-hour session boundary.

The default is 47 hours 45 minutes, followed by a 10-second pause.
A manual stop remains stopped.
]]
end

function script_defaults(settings)
    obs.obs_data_set_default_int(settings, "cycle_seconds", DEFAULT_CYCLE_SECONDS)
    obs.obs_data_set_default_int(
        settings,
        "restart_delay_seconds",
        DEFAULT_RESTART_DELAY_SECONDS
    )
end

function script_properties()
    local properties = obs.obs_properties_create()

    obs.obs_properties_add_int(
        properties,
        "cycle_seconds",
        "Restart interval (seconds)",
        60,
        604800,
        1
    )
    obs.obs_properties_add_int(
        properties,
        "restart_delay_seconds",
        "Pause before restart (seconds)",
        5,
        300,
        1
    )

    return properties
end

function script_update(settings)
    cycle_seconds = obs.obs_data_get_int(settings, "cycle_seconds")
    restart_delay_seconds = obs.obs_data_get_int(
        settings,
        "restart_delay_seconds"
    )

    if cycle_seconds < 60 then
        cycle_seconds = DEFAULT_CYCLE_SECONDS
    end
    if restart_delay_seconds < 5 then
        restart_delay_seconds = DEFAULT_RESTART_DELAY_SECONDS
    end

    if obs.obs_frontend_streaming_active() then
        reset_cycle_from_now()
    end
end

function script_load(settings)
    obs.obs_frontend_add_event_callback(on_frontend_event)
    obs.timer_add(on_timer, 1000)
    log_info("Script loaded.")

    if obs.obs_frontend_streaming_active() then
        reset_cycle_from_now()
    end
end

function script_unload()
    obs.timer_remove(on_timer)
    obs.obs_frontend_remove_event_callback(on_frontend_event)
end
