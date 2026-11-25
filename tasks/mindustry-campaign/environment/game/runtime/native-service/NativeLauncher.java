package benchmark;

import arc.Core;
import arc.Events;
import arc.scene.ui.TextField;
import arc.scene.ui.TextButton;
import arc.scene.Element;
import arc.scene.event.InputEvent;
import arc.scene.event.InputListener;
import arc.input.KeyCode;
import arc.util.Time;
import arc.util.serialization.JsonReader;
import arc.util.serialization.JsonValue;
import arc.util.serialization.JsonWriter;
import mindustry.Vars;
import mindustry.core.GameState.State;
import mindustry.core.Version;
import mindustry.core.Platform;
import mindustry.desktop.DesktopLauncher;
import mindustry.game.EventType.ClientLoadEvent;
import mindustry.game.EventType.GameOverEvent;
import mindustry.game.EventType.Trigger;
import mindustry.service.Achievement;
import mindustry.service.GameService;
import mindustry.service.SStat;
import mindustry.ui.fragments.ConsoleFragment;

import java.io.IOException;
import java.io.StringWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;

/** Storage compatibility and aggregate clock only; native award predicates stay in the JAR. */
public final class NativeLauncher {
    static final Path directory = Path.of("/state/benchmark-private");
    static final String gameHash = "36d94941a1639478ad1d9be25a9d802c6f77552f400c6da0338b7b019f63a0d1";
    static final String attempt = System.getenv().getOrDefault("BENCHMARK_ATTEMPT_ID",
        java.util.UUID.randomUUID().toString());
    static final String task = System.getenv().getOrDefault("BENCHMARK_TASK", "offline-achievements");
    static final boolean campaign = task.equals("mindustry-campaign");
    static final TreeSet<String> eligible = new TreeSet<>();
    static OfflineService service;
    static double playSeconds;
    static boolean hasPlayed;
    static boolean frozen;
    static String lastFinishChallenge;
    static String lastClockChallenge;
    static String frozenSnapshot;
    static long policySequence;
    static final String survivalStart = System.getenv("BENCHMARK_SURVIVAL_START");
    static final String mazeHash = "a7b4443a153c1e0100ec0ab23d2536272f9dfa864fea3c340110f329523b51fb";
    static String initialLocale;
    static JsonValue continuation;

    public static void main(String[] args) throws Exception {
        if (!"preventive-v2".equals(System.getenv("BENCHMARK_PROFILE")) &&
            "1".equals(System.getenv("BENCHMARK_NATIVE_WITNESS"))) {
            // Prepare the evaluator while the controller still owns bootstrap,
            // before desktop readiness or any agent input is possible. Runtime
            // evaluator breakpoints never suspend the game thread.
            Class.forName("mindustry.mod.Scripts");
        }
        if (attempt == null || attempt.isBlank()) throw new IllegalStateException("Missing attempt ID");
        if (!java.util.Set.of("mindustry-campaign", "offline-achievements", "survival").contains(task)) {
            throw new IllegalStateException("Unknown task kind");
        }
        if (survivalStart != null &&
            (!task.equals("survival") || !survivalStart.equals("maze-native-v160.7"))) {
            throw new IllegalStateException("Unknown or cross-task survival start");
        }
        boolean continuing = "1".equals(System.getenv("BENCHMARK_CONTINUATION"));
        if (Files.exists(directory) && !continuing) {
            throw new IllegalStateException("Refusing inherited attempt state");
        }
        String actualHash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
            .digest(Files.readAllBytes(Path.of("/opt/benchmark/Mindustry.jar"))));
        if (!gameHash.equals(actualHash)) throw new IllegalStateException("Wrong game JAR");
        if (campaign) {
            JsonValue contract = new JsonReader().parse(Files.readString(Path.of("/opt/benchmark/task-contract.json")));
            if (contract.getInt("version") != 3 || !task.equals(contract.getString("task"))) {
                throw new IllegalStateException("Wrong campaign contract");
            }
            for (JsonValue value : contract.get("eligible_ids")) {
                if (!eligible.add(Achievement.valueOf(value.asString()).name())) {
                    throw new IllegalStateException("Duplicate achievement target");
                }
            }
            if (eligible.isEmpty()) throw new IllegalStateException("Missing achievement targets");
        }
        if (continuing) {
            continuation = new JsonReader().parse(Files.readString(directory.resolve("restart.request")));
            if (!(campaign || task.equals("offline-achievements")) || !attempt.equals(continuation.getString("attempt_id")) ||
                !gameHash.equals(continuation.getString("game_sha256")) ||
                Files.exists(directory.resolve("final.json")) ||
                Files.exists(Path.of("/state/benchmark-input.closed"))) {
                throw new IllegalStateException("Invalid or terminated language continuation");
            }
            String storageHash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(Files.readAllBytes(directory.resolve("native-storage.json"))));
            if (!storageHash.equals(continuation.getString("storage_sha256"))) {
                throw new IllegalStateException("Language continuation storage changed");
            }
            playSeconds = continuation.getDouble("play_seconds");
            hasPlayed = continuation.getBoolean("has_played");
            policySequence = continuation.getLong("policy_sequence");
            if (!Double.isFinite(playSeconds) || playSeconds < 0 || policySequence < 0) {
                throw new IllegalStateException("Invalid continuation clock/history");
            }
            Files.delete(directory.resolve("restart.request")); // one use, before any native initialization
        } else {
            Files.createDirectories(directory);
        }
        StringWriter identity = new StringWriter();
        new JsonWriter(identity).object().set("attempt_id", attempt).set("task", task).pop().close();
        atomicWrite(directory.resolve("identity.json"), identity.toString());
        service = new OfflineService();
        if (continuing) {
            JsonValue stored = new JsonReader().parse(Files.readString(directory.resolve("native-storage.json")));
            if (!attempt.equals(stored.getString("attempt_id"))) throw new IllegalStateException("Stale storage");
            for (JsonValue item : stored.get("achieved_ids")) {
                service.achieved.add(Achievement.valueOf(item.asString()).name());
            }
            for (JsonValue stat : stored.get("stats")) {
                service.stats.put(SStat.valueOf(stat.name).name(), stat.asInt());
            }
            if (campaign) {
                for (JsonValue item : stored.get("achievement_times")) {
                    double seconds = item.asDouble();
                    if (!Double.isFinite(seconds) || seconds < 0 || seconds > playSeconds) {
                        throw new IllegalStateException("Invalid achievement milestone");
                    }
                    service.achievementTimes.put(Achievement.valueOf(item.name).name(), seconds);
                }
                if (!service.achievementTimes.keySet().equals(service.achieved)) {
                    throw new IllegalStateException("Incomplete achievement milestone history");
                }
            }
        }
        Vars.service = service;
        // This listener runs before native GameService update listeners/simulation.
        Events.run(Trigger.update, () -> {
            checkFinish();
            checkClockRequest();
            if (campaign && !frozen && service.achieved.containsAll(eligible)) {
                try {
                    freeze("objective_complete");
                } catch (IOException error) {
                    throw new RuntimeException("Cannot export full achievement completion", error);
                }
            }
        });
        Events.run(Trigger.afterGameUpdate, () -> {
            if (!frozen) {
                hasPlayed = true;
                playSeconds += Time.delta / 60.0;
            }
        });
        Events.on(GameOverEvent.class, event -> {
            // Use the real native losing event; player respawn and an individual
            // core's destruction are not survival termination conditions.
            if (task.equals("survival") && event.winner != Vars.state.rules.defaultTeam &&
                Vars.state.rules.defaultTeam.cores().isEmpty() && hasPlayed) {
                try {
                    freeze("natural_defeat");
                } catch (IOException error) {
                    throw new RuntimeException("Cannot export native defeat", error);
                }
            }
        });
        service.init(); // Register unchanged native predicates exactly once on ClientLoadEvent.
        Events.on(ClientLoadEvent.class, event -> {
            if (Vars.service != service) throw new IllegalStateException("Native service replaced");
            initialLocale = Core.settings.getString("locale", "default");
            if (continuation != null && !initialLocale.equals(continuation.getString("locale"))) {
                throw new IllegalStateException("Language continuation locale changed");
            }
            // Observed production runs use the unchanged native console. The
            // external witness records evaluator exposure without stopping play.
            // Retain the preventive console only for old unobserved QA fixtures.
            if ("preventive-v2".equals(System.getenv("BENCHMARK_PROFILE")) ||
                !"1".equals(System.getenv("BENCHMARK_NATIVE_WITNESS"))) {
                Vars.ui.consolefrag.remove();
                Vars.ui.consolefrag = new RestrictedConsole();
                Vars.ui.consolefrag.build(Core.scene.root);
            }
            // Keep native platform behavior except arbitrary OS file dialogs.
            // Internal campaign saves/map/schematic creation do not use this API.
            Platform originalPlatform = Vars.platform;
            Vars.platform = (Platform) java.lang.reflect.Proxy.newProxyInstance(
                Platform.class.getClassLoader(), new Class<?>[]{Platform.class},
                (proxy, method, arguments) -> {
                    if (method.getName().equals("showFileChooser")) {
                        recordBlocked("file_import_export");
                        return null;
                    }
                    try {
                        return method.invoke(originalPlatform, arguments);
                    } catch (java.lang.reflect.InvocationTargetException error) {
                        throw error.getCause();
                    }
                });
            Core.scene.root.addCaptureListener(new InputListener() {
                private boolean deny(InputEvent event) {
                    for (Element actor = event.targetActor; actor != null; actor = actor.parent) {
                        if (actor instanceof TextButton button) {
                            String category = null;
                            String label = button.getText().toString();
                            if (!frozen && (campaign || task.equals("offline-achievements")) && Vars.state.isMenu() &&
                                label.equals(Core.bundle.get("quit")) &&
                                !initialLocale.equals(Core.settings.getString("locale", "default"))) {
                                event.cancel();
                                Core.app.post(NativeLauncher::restartForLanguage);
                                return true;
                            }
                            if (label.equals(Core.bundle.get("mods"))) category = "mod_management";
                            if (label.equals(Core.bundle.get("loadgame"))) category = "manual_save_load";
                            if (label.equals(Core.bundle.get("data.import"))) category = "profile_import";
                            if (label.equals(Core.bundle.get("data.openfolder"))) category = "external_folder";
                            if (label.equals(Core.bundle.get("schematic.import"))) category = "schematic_import";
                            if (task.equals("survival") && hasPlayed) {
                                for (String key : new String[]{"quit", "save.quit", "abandon"}) {
                                    if (label.equals(Core.bundle.get(key))) category = "survival_exit";
                                }
                            }
                            for (String key : new String[]{"settings.cleardata", "settings.clearplanetdata",
                                "settings.clearsaves", "settings.clearresearch", "settings.clearcampaignsaves"}) {
                                if (label.equals(Core.bundle.get(key))) category = "profile_reset";
                            }
                            if (category != null) {
                                event.cancel();
                                recordBlocked(category);
                                return true;
                            }
                        }
                    }
                    return false;
                }

                @Override public boolean touchDown(InputEvent event, float x, float y,
                                                   int pointer, KeyCode button) {
                    return deny(event);
                }

                @Override public boolean keyDown(InputEvent event, KeyCode key) {
                    return (key == KeyCode.enter || key == KeyCode.space) && deny(event);
                }
            });
            service.storeStats();
            if (continuation != null) {
                try {
                    Files.writeString(directory.resolve("restart-history.jsonl"),
                        continuation.toJson(JsonWriter.OutputType.json) + "\n",
                        StandardOpenOption.CREATE, StandardOpenOption.APPEND);
                    Files.delete(directory.resolve("restart.in-progress"));
                } catch (IOException error) {
                    throw new RuntimeException("Cannot acknowledge language continuation", error);
                }
            }
            if (survivalStart != null) Core.app.post(NativeLauncher::startApprovedSurvival);
        });
        DesktopLauncher.main(args);
    }

    static void restartForLanguage() {
        if (frozen || !Vars.state.isMenu() || Files.exists(directory.resolve("final.json")) ||
            Files.exists(directory.resolve("finish.request")) ||
            Files.exists(Path.of("/state/benchmark-input.closed"))) {
            recordBlocked("restart_after_termination");
            return;
        }
        try {
            service.storeStats();
            Core.settings.forceSave();
            String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(Files.readAllBytes(directory.resolve("native-storage.json"))));
            StringWriter text = new StringWriter();
            new JsonWriter(text).object().set("attempt_id", attempt).set("game_sha256", gameHash)
                .set("play_seconds", playSeconds).set("has_played", hasPlayed)
                .set("policy_sequence", policySequence).set("storage_sha256", hash)
                .set("locale", Core.settings.getString("locale")).pop().close();
            atomicWrite(directory.resolve("restart.request"), text.toString());
            atomicWrite(directory.resolve("restart.in-progress"), attempt);
            // Reserved supervisor exit, not an evaluated retry or general crash restart.
            System.exit(73);
        } catch (Exception error) {
            throw new RuntimeException("Cannot checkpoint native language change", error);
        }
    }

    static void startApprovedSurvival() {
        // Use the same native entry point as Custom Game's Play button. No
        // authored layout, granted research/resources, or changed wave rules.
        var map = Vars.maps.all().find(candidate ->
            candidate.file.path().equals("maps/default/maze.msav"));
        if (map == null) throw new IllegalStateException("Approved Maze asset missing");
        try {
            String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(map.file.readBytes()));
            if (!mazeHash.equals(hash)) throw new IllegalStateException("Approved Maze asset changed");
        } catch (java.security.NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        }
        Events.run(Trigger.newGame, () -> {
            if (Files.exists(directory.resolve("survival-start.json"))) {
                throw new IllegalStateException("Refusing a second survival start");
            }
            if (Vars.state.map != map || !Vars.state.rules.waves || Vars.state.rules.attackMode ||
                Vars.state.rules.winWave != 0 || Vars.state.rules.defaultTeam.id != 1) {
                throw new IllegalStateException("Native survival start differs from approved preset");
            }
            try {
                StringWriter text = new StringWriter();
                new JsonWriter(text).object().set("attempt_id", attempt)
                    .set("map_id", "maps/default/maze.msav").set("map_sha256", mazeHash)
                    .set("preset", "unchanged-native-survival").set("seed", null)
                    .set("team_id", Vars.state.rules.defaultTeam.id)
                    .set("win_wave", Vars.state.rules.winWave).set("waves", Vars.state.rules.waves)
                    .set("attack_mode", Vars.state.rules.attackMode)
                    .set("width", Vars.world.width()).set("height", Vars.world.height())
                    .set("starting_copper", Vars.state.rules.defaultTeam.core().items
                        .get(mindustry.content.Items.copper))
                    .set("core_count", Vars.state.rules.defaultTeam.cores().size).pop().close();
                atomicWrite(directory.resolve("survival-start.json"), text.toString());
            } catch (IOException error) {
                throw new RuntimeException("Cannot persist native start identity", error);
            }
        });
        Vars.control.playMap(map, map.applyRules(mindustry.game.Gamemode.survival));
    }

    static final class RestrictedConsole extends ConsoleFragment {
        @Override public void toggle() {
            if (!open()) {
                super.toggle(); // unchanged native Trigger.openConsole
                return;
            }
            boolean nonempty = false;
            for (var child : getChildren()) {
                if (child instanceof TextField field && !field.getText().trim().isEmpty()) {
                    nonempty = true;
                }
            }
            // hide() clears the input/history and focus without invoking the
            // private native sendMessage()/runConsole() path.
            hide();
            if (nonempty) {
                recordBlocked("console_execution");
                addMessage("[scarlet]Console command execution is disabled in this task.");
            }
        }
    }

    static void recordBlocked(String category) {
        try {
            StringWriter text = new StringWriter();
            JsonWriter json = new JsonWriter(text);
            json.object().set("attempt_id", attempt).set("event_id", "native-" + (++policySequence))
                .set("category", category).set("decision", "blocked")
                .set("play_seconds", playSeconds).pop().close();
            Files.writeString(directory.resolve("policy-events.jsonl"), text + "\n",
                StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException error) {
            throw new RuntimeException("Cannot persist observable policy event", error);
        }
    }

    static void checkFinish() {
        try {
            if (frozen) {
                if (Vars.state.isGame()) Vars.state.set(State.paused);
            }
            Path request = directory.resolve("finish.request");
            if (!Files.exists(request)) return;
            JsonValue data = new JsonReader().parse(Files.readString(request));
            if (!attempt.equals(data.getString("attempt_id"))) throw new IllegalStateException("Stale finish");
            String challenge = data.getString("challenge");
            if (!challenge.matches("[0-9a-f]{32}")) throw new IllegalStateException("Invalid challenge");
            if (challenge.equals(lastFinishChallenge)) return;
            String reason = data.getString("reason");
            if (!java.util.Set.of("agent_finish", "natural_defeat", "stagnation", "policy_violation",
                "infrastructure_error", "external_stop", "objective_complete", "subscription_limit").contains(reason)) {
                throw new IllegalStateException("Unknown ending");
            }
            freeze(reason);
            writeFinalReply(challenge);
            lastFinishChallenge = challenge;
        } catch (IOException error) {
            throw new RuntimeException("Native finalization failed", error);
        }
    }

    static void freeze(String reason) throws IOException {
        if (frozen) return;
        frozen = true;
        if (Vars.state.isGame()) Vars.state.set(State.paused);
        frozenSnapshot = service.buildFinal(reason);
        writeFinalReply(null);
    }

    static void writeFinalReply(String challenge) throws IOException {
        String nonce = challenge == null ? "null" : "\"" + challenge + "\"";
        atomicWrite(directory.resolve("final.json"),
            frozenSnapshot.substring(0, frozenSnapshot.length() - 1) + ",\"challenge\":" + nonce + "}");
    }

    static void atomicWrite(Path target, String text) throws IOException {
        Path temporary = target.resolveSibling(target.getFileName() + ".tmp");
        Files.writeString(temporary, text);
        Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }

    static void checkClockRequest() {
        try {
            Path request = directory.resolve("clock.request");
            if (!Files.exists(request)) return;
            JsonValue data = new JsonReader().parse(Files.readString(request));
            if (!attempt.equals(data.getString("attempt_id"))) throw new IllegalStateException("Stale clock request");
            String challenge = data.getString("challenge");
            if (!challenge.matches("[0-9a-f]{32}")) throw new IllegalStateException("Invalid challenge");
            if (challenge.equals(lastClockChallenge)) return;
            StringWriter text = new StringWriter();
            JsonWriter json = new JsonWriter(text);
            json.object().set("attempt_id", attempt).set("play_seconds", playSeconds)
                .set("finalized", frozen).set("has_played", hasPlayed)
                .set("protected_work", protectedWork());
            if (data.getString("operation", "read_clock").equals("read_progress")) {
                json.array("achieved_ids");
                for (String name : service.achieved) json.value(name);
                json.pop();
                if (campaign) service.writeMilestones(json);
            }
            json.set("challenge", challenge).pop().close();
            atomicWrite(directory.resolve("clock.json"), text.toString());
            lastClockChallenge = challenge;
        } catch (IOException error) {
            throw new RuntimeException("Cannot publish trusted clock", error);
        }
    }

    static boolean protectedWork() {
        if (frozen) return false;
        // Conservative context protection, not an assertion of productive intent.
        // Idle editing may therefore remain live indefinitely; no hidden horizon.
        if (Files.exists(directory.resolve("restart.in-progress"))) return true;
        if (Vars.ui == null || Core.scene == null) return false;
        if (Vars.ui.editor.isShown() || Vars.ui.schematics.isShown() || Vars.ui.logic.isShown() ||
            Core.scene.getKeyboardFocus() instanceof TextField) return true;
        if (Vars.control != null && Vars.control.input != null &&
            (Vars.control.input.lastSchematic != null || Vars.control.input.selectPlans.any())) return true;
        return Vars.player != null && !Vars.player.dead() && Vars.player.unit().plans().size > 0;
    }

    static final class OfflineService extends GameService {
        final TreeSet<String> achieved = new TreeSet<>();
        final TreeMap<String, Integer> stats = new TreeMap<>();
        final TreeMap<String, Double> achievementTimes = new TreeMap<>();

        @Override public boolean enabled() { return true; }
        @Override public synchronized boolean isAchieved(String name) { return achieved.contains(name); }
        @Override public synchronized void completeAchievement(String name) {
            Achievement.valueOf(name); // Native enum names only; no externally supplied award interface.
            if (!frozen && achieved.add(name) && campaign) achievementTimes.put(name, playSeconds);
        }
        @Override public synchronized void clearAchievement(String name) {
            if (!frozen) {
                achieved.remove(name);
                achievementTimes.remove(name);
            }
        }
        @Override public synchronized int getStat(String name, int fallback) { return stats.getOrDefault(name, fallback); }
        @Override public synchronized void setStat(String name, int amount) {
            SStat.valueOf(name);
            if (!frozen) stats.put(name, amount);
        }
        @Override public synchronized void storeStats() {
            try {
                StringWriter text = new StringWriter();
                JsonWriter json = new JsonWriter(text);
                json.object().set("attempt_id", attempt).array("achieved_ids");
                for (String name : achieved) json.value(name);
                json.pop().object("stats");
                for (Map.Entry<String, Integer> stat : stats.entrySet()) json.set(stat.getKey(), stat.getValue());
                json.pop();
                if (campaign) writeMilestones(json);
                json.pop().close();
                atomicWrite(directory.resolve("native-storage.json"), text.toString());
            } catch (IOException error) {
                throw new RuntimeException("Cannot persist native achievements", error);
            }
        }

        synchronized String buildFinal(String reason) throws IOException {
            StringWriter text = new StringWriter();
            JsonWriter json = new JsonWriter(text);
            json.object().set("format_version", campaign ? 2 : 1).set("attempt_id", attempt)
                .set("game_version", "v" + Version.buildString()).set("game_sha256", gameHash)
                .set("play_seconds", playSeconds).set("finalized", true).set("reason", reason)
                .set("team_id", Vars.state.rules.defaultTeam.id)
                .set("cores_alive", Vars.state.rules.defaultTeam.cores().size)
                .set("game_over", Vars.state.gameOver).set("has_played", hasPlayed)
                .array("known_ids");
            for (Achievement achievement : Achievement.all) json.value(achievement.name());
            json.pop().array("achieved_ids");
            for (String name : achieved) json.value(name);
            json.pop();
            if (campaign) writeMilestones(json);
            json.pop().close();
            storeStats();
            return text.toString();
        }

        synchronized void writeMilestones(JsonWriter json) throws IOException {
            json.object("achievement_times");
            for (Map.Entry<String, Double> entry : achievementTimes.entrySet()) {
                json.set(entry.getKey(), entry.getValue());
            }
            json.pop();
        }
    }
}
