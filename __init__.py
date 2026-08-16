from pathlib import Path

from mods_base import BoolOption, SETTINGS_DIR, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

# A BL1E (GOTY Enhanced) port of Miner Of Worlds and RedxYeti's original BL1
# "Auto-Pickup SDK" mod (https://github.com/MOW531/MOW531-BL1-SDK-Mods,
# GPL-3.0). See README.md for what changed and why this is a separate
# GPL-3.0 project rather than a patch to the original.
#
# Rewritten from scratch 2026-08-15. The previous version tried to make BL1E's
# own native auto-pickup systems collect everything, by flagging each
# pickup's ItemDefinition (ItemDefinition.bAutomaticallyPickup) or sweeping
# the whole ItemDefinition table. That turned out to be fighting two
# different, only partly-understood native mechanisms at once
# (WillowPlayerController.TouchedPickupable checks bAutomaticallyPickup only
# on physical touch; ServerAutoPickupSomething sweeps by proximity using a
# *different* flag, WillowInventory.bAutoLoot) - and neither one reliably
# collected loot ejected in a burst from a lootable container (e.g. opening
# a washer/locker), even once the ItemDefinition was correctly flagged.
#
# This version does not rely on either native system at all. It hooks
# WillowPickup:SpawnPickupParticles - proven to fire for every pickup,
# whether it's lying on the ground or was just ejected from a container -
# and for enabled categories, directly runs the pickup's own real collection
# sequence (HasRoomInInventoryFor -> PickupQuery -> coop clone -> client mesh
# spawn -> GiveTo). That sequence is copied from this mod's own prior
# WillowPlayerController:TouchedPickupable hook, which used the identical
# calls successfully - it is simply invoked unconditionally now instead of
# only when tied to a tracked "used" interactive object.

pickup_ammo = BoolOption(
    "Pickup Ammo",
    True,
    description="Automatically collect ammo, from the ground or a container.",
)
pickup_currency = BoolOption(
    "Pickup Currency & Valuables",
    True,
    description="Automatically collect cash, Bobbleheads, and Skag Pearls.",
)
pickup_health = BoolOption(
    "Pickup Health Vials",
    True,
    description="Automatically collect health vials.",
)
pickup_mission_collectibles = BoolOption(
    "Pickup Quest Collectibles",
    True,
    description="Automatically collect mission tally pickups, e.g. Bottle of Booze.",
)

# Item names (short Name, not full object path - all that's available off a
# spawned pickup) mapped to the option that gates them. Verified directly
# against this install's own data (grepped
# WillowGame/CookedPC/Packages/GameData/Inventory/gd_ammodrops.upk and
# gd_currency.upk for every AmmoDrop_/Currency/Bobblehead/SkagPearl object)
# rather than assumed.
CURRENCY_NAMES = frozenset((
    "Currency",
    "Currency_big",
    "Currency_PrizeFighter",
    "Bobblehead",
    "SkagPearl",
))
HEALTH_VIAL_NAMES = frozenset((
    "HealthVial_1",
    "HealthVial_2",
    "HealthVial_3",
    "HealthVial_4",
    "HealthVial_5",
))


def option_for(item_def, is_mission_item: bool, inventory_class_name: str) -> BoolOption | None:
    """Which toggle, if any, governs auto-collecting this pickup.

    Mission items are handled separately from everything else: the only
    mission items ever matched here are ones of class WillowUsableItem - the
    same instant-consume-on-pickup class ammo/currency/health vials belong to
    - which by construction means they're a pure tally (e.g. "Bottle of
    Booze: 4/24") rather than a unique object a script expects to still find
    sitting in the world. A unique carried quest item is never this class, so
    it's never matched here and always stays manual, mission-tracked or not.
    """
    if is_mission_item:
        if inventory_class_name == "WillowUsableItem":
            return pickup_mission_collectibles
        return None

    name = str(item_def.Name)
    if name.startswith("AmmoDrop_"):
        return pickup_ammo
    if name in CURRENCY_NAMES:
        return pickup_currency
    if name in HEALTH_VIAL_NAMES:
        return pickup_health
    return None


# SawPickupable (added below) is the native aim/raycast "you're looking at
# this" event - unlike TouchedPickupable/SpawnPickupParticles/PickupAtRest,
# which each fire once per pickup, this can plausibly re-fire every tick for
# as long as the player keeps aiming at the same still-uncollectible item.
# Message-text-based throttling (same technique already used in
# AutoLootBL1E, chosen there for the same reason) dedupes identical repeat
# log lines without needing the pickup actor's Python-side identity to stay
# stable across separate hook calls, which nothing in this codebase relies
# on elsewhere.
_recent_log_messages: dict[str, float] = {}
LOG_REPEAT_COOLDOWN = 5.0


def log_throttled(now: float, message: str) -> None:
    last = _recent_log_messages.get(message)
    if last is not None and now - last < LOG_REPEAT_COOLDOWN:
        return
    _recent_log_messages[message] = now
    logging.info(message)


def collect_pickup(controller, pickupable) -> tuple[bool, str]:
    """Give a pickup straight to the player, the same way a manual pickup would.

    Sequence copied from this mod's own working
    WillowPlayerController:TouchedPickupable hook (HasRoomInInventoryFor,
    then PickupQuery, then the coop-clone/client-mesh/GiveTo trio) - all four
    calls already proven safe and correct in this exact codebase, just no
    longer gated behind being triggered by a tracked interactive-object use.

    Returns (success, reason) - reason names which gate stopped it, so a
    failure is always traceable to a specific engine call rather than just
    "didn't work".
    """
    if not controller.HasRoomInInventoryFor(pickupable):
        return False, "HasRoomInInventoryFor=False"
    if not controller.WorldInfo.Game.PickupQuery(controller.Pawn, pickupable):
        return False, "PickupQuery=False"
    if controller.ShouldUseCoopRange(pickupable):
        controller.CloneAndGiveToCoopPawns(pickupable, False)
    controller.ClientSpawnPickupableMesh(pickupable)
    pickupable.GiveTo(controller.Pawn, False)
    return True, "GiveTo"


def try_auto_collect(obj: UObject, trigger: str) -> None:
    """Attempt to auto-collect one pickup actor, logging the outcome.

    Called from two different hooks (see below) rather than once, because a
    single attempt right at spawn was confirmed - via a user's manual [F]
    pickup working seconds later on an item this had just logged
    PickupQuery=False for - to be a false negative, not a real refusal:
    PickupQuery evidently reads some piece of the pickup/inventory state
    (e.g. the ammo drop's randomized quantity, assigned through
    AmmoDropWeightAttributeValueResolver) that isn't necessarily settled the
    instant the pickup is spawned/associated, which is when
    SpawnPickupParticles fires. Retrying once more when the pickup physically
    settles (PickupAtRest - always reached, since every dropped pickup gets a
    physics impulse on spawn) gives that state a second, later chance to be
    ready, without guessing further at what specifically was still pending.
    """
    inventory = obj.Inventory
    if inventory is None or inventory.DefinitionData is None or inventory.Class is None:
        return
    # Only WillowUsableItem's DefinitionData struct (ItemDefinitionData) has
    # an ItemDefinition field at all - WillowWeapon's is WeaponDefinitionData,
    # which does not, and every category this mod ever acts on (ammo,
    # currency, health vials, mission tally pickups) is WillowUsableItem
    # anyway. Confirmed crashing in play: AttributeError 'WeaponDefinitionData'
    # object has no attribute 'ItemDefinition', thrown every time a weapon
    # pickup (e.g. one dropped by another mod) spawned its particles, because
    # this used to read .DefinitionData.ItemDefinition unconditionally before
    # checking the class at all.
    class_name = str(inventory.Class.Name)
    if class_name != "WillowUsableItem":
        return
    item_def = inventory.DefinitionData.ItemDefinition
    if item_def is None:
        return

    item_name = str(item_def.Name)
    is_mission_item = bool(item_def.bMissionItem)
    option = option_for(item_def, is_mission_item, class_name)
    now = obj.WorldInfo.TimeSeconds

    # Trace logging: this hook was rewritten from scratch on 2026-08-15 and,
    # until now, only ever logged the exception case - a miss that reached no
    # option at all, or one whose option was simply off, left no trace
    # anywhere. Every pickup event for a WillowUsableItem is logged here so a
    # "didn't get picked up" report can be matched to an exact gate instead
    # of re-guessed from source. Throttled (see log_throttled) since the
    # "seen" trigger can repeat every tick for an unchanging outcome.
    if option is None:
        log_throttled(
            now,
            f"[Autopickup] trace[{trigger}]: {item_name} (mission_item={is_mission_item})"
            " -> no matching option, left for manual pickup",
        )
        return
    if not option.value:
        log_throttled(
            now,
            f"[Autopickup] trace[{trigger}]: {item_name} -> option '{option.display_name}'"
            " is off, skipping",
        )
        return

    controller = get_pc()
    if controller is None or controller.Pawn is None:
        log_throttled(now, f"[Autopickup] trace[{trigger}]: {item_name} -> no controller/pawn yet, skipping")
        return

    try:
        success, reason = collect_pickup(controller, obj)
        log_throttled(now, f"[Autopickup] trace[{trigger}]: {item_name} -> collect_pickup {reason}")
    except Exception as ex:  # noqa: BLE001
        logging.warning(f"[Autopickup] could not auto-collect {item_name} ({trigger}): {ex!r}")


@hook("WillowGame.WillowPickup:SpawnPickupParticles", Type.POST)
def SpawnPickupParticles(obj: UObject, __args: WrappedStruct, __ret: any, __func: BoundFunction) -> None:
    try_auto_collect(obj, "spawn")


@hook("WillowGame.WillowPickup:PickupAtRest", Type.POST)
def PickupAtRest(obj: UObject, __args: WrappedStruct, __ret: any, __func: BoundFunction) -> None:
    # obj.Inventory reads None once GiveTo() has already destroyed this
    # pickup (e.g. the spawn-time attempt above already succeeded) - the same
    # top-of-function None checks in try_auto_collect that guard every other
    # caller apply here too, so a second, redundant collect attempt on an
    # already-collected/destroyed actor is never made.
    try_auto_collect(obj, "rest")


@hook("WillowGame.WillowPlayerController:TouchedPickupable", Type.POST)
def TouchedPickupable(obj: UObject, __args: WrappedStruct, __ret: any, __func: BoundFunction) -> None:
    # The spawn/rest attempts above are tied to the PICKUP's own one-shot
    # lifecycle (spawn, physics settling) - each fires once, ever, regardless
    # of anything about the player. Confirmed in play: a pickup that failed
    # both of those (player was at capacity for it at the time) then sat on
    # the ground forever, un-retried, even after the player later had room
    # (emptied their reserve, then walked back to the same still-lying item)
    # - neither event has any reason to fire again just because the PLAYER's
    # state changed.
    #
    # TouchedPickupable is the native engine's own answer to that: it fires
    # whenever a pawn is close enough to physically touch a pickup, and -
    # per DroppedPickup.ValidTouch/RecheckValidTouch in the engine source -
    # the engine itself only allows the touch through once its own
    # PickupQuery already passes, re-checking on a timer for as long as a
    # pawn remains in contact. So this hook only ever fires when the game
    # has just confirmed the pickup is currently collectible - exactly the
    # "try again, player state may have changed" signal the first two
    # attempts can't provide, and it naturally re-fires on every later
    # approach, not just the first.
    #
    # obj.GetCurrentPickupable() (not the Pickup argument directly) matches
    # this mod's own prior TouchedPickupable hook, already proven to work
    # safely with this exact call sequence.
    pickupable = obj.GetCurrentPickupable()
    if pickupable is None:
        return
    try_auto_collect(pickupable, "touch")


@hook("WillowGame.WillowPlayerController:SawPickupable", Type.POST)
def SawPickupable(obj: UObject, __args: WrappedStruct, __ret: any, __func: BoundFunction) -> None:
    # TouchedPickupable (above) turned out not to be enough on its own:
    # confirmed in play that it never fires at all for a pickup the player
    # never physically walks into - e.g. sitting up on a chest/altar, aimed
    # at from a short distance rather than touched. [F] PICK UP in that case
    # is driven by a completely different native path: a per-tick aim
    # raycast (not present in the decompiled script - native-only) calls
    # this event, which sets CurrentSeenPickupable, which GetCurrentPickupable
    # prioritizes over CurrentTouchedPickupable. This is what PickupSomething
    # (the exec function bound to the F key) itself reads, so hooking this
    # covers exactly the pickups the player is aiming at with the prompt on
    # screen - the case TouchedPickupable structurally cannot reach.
    #
    # May fire every tick while the player keeps aiming at the same pickup -
    # try_auto_collect's own log_throttled call keeps that from spamming the
    # log; the actual HasRoomInInventoryFor/PickupQuery/GiveTo calls are left
    # unthrottled since they are cheap reads, not a scan, and once GiveTo
    # succeeds the pickup is destroyed so no further attempts land on it.
    pickupable = obj.GetCurrentPickupable()
    if pickupable is None:
        return
    try_auto_collect(pickupable, "seen")


# Gets populated from `build_mod` below
__version__: str
__version_info__: tuple[int, ...]

build_mod(
    options=[pickup_ammo, pickup_currency, pickup_health, pickup_mission_collectibles],
    hooks=[SpawnPickupParticles, PickupAtRest, TouchedPickupable, SawPickupable],
    settings_file=Path(f"{SETTINGS_DIR}/AutoPickupSDK.json"),
)

logging.info(f"Auto-Pickup SDK Loaded: {__version__}, {__version_info__}")
