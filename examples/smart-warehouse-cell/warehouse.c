#include <math.h>
#include <stdlib.h>
#include <string.h>

typedef void* fmi2Component;
typedef void* fmi2Environment;
typedef const char* fmi2String;
typedef double fmi2Real;
typedef int fmi2Integer;
typedef int fmi2Boolean;
typedef unsigned char fmi2Byte;
typedef unsigned int fmi2ValueReference;
typedef enum { fmi2OK = 0, fmi2Warning = 1, fmi2Discard = 2, fmi2Error = 3, fmi2Fatal = 4, fmi2Pending = 5 } fmi2Status;
typedef enum { fmi2ModelExchange = 0, fmi2CoSimulation = 1 } fmi2Type;
typedef enum { fmi2DoStepStatus = 0, fmi2PendingStatus = 1, fmi2LastSuccessfulTime = 2, fmi2Terminated = 3 } fmi2StatusKind;
typedef void* fmi2FMUstate;
typedef void (*fmi2CallbackLogger)(fmi2Component, fmi2String, fmi2Status, fmi2String, fmi2String, ...);
typedef void* (*fmi2CallbackAllocateMemory)(size_t, size_t);
typedef void (*fmi2CallbackFreeMemory)(void*);
typedef void (*fmi2StepFinished)(fmi2Component, fmi2Status);
typedef struct {
    fmi2CallbackLogger logger;
    fmi2CallbackAllocateMemory allocateMemory;
    fmi2CallbackFreeMemory freeMemory;
    fmi2StepFinished stepFinished;
    fmi2Environment componentEnvironment;
} fmi2CallbackFunctions;

typedef struct {
    fmi2CallbackFunctions callbacks;
    fmi2Real time;
    fmi2Real speed;
    fmi2Real agvPosition;
    fmi2Real robotAngle;
    fmi2Real forkHeight;
    fmi2Real inventoryLevel;
    fmi2Real pickupProgress;
    fmi2Real transferPackageX;
    fmi2Real transferPackageY;
    fmi2Real transferPackageZ;
    fmi2Real loaderAngle;
    fmi2Real loaderReach;
    fmi2Real beltPhase;
    fmi2Real packageSizeX;
    fmi2Real packageSizeY;
    fmi2Real packageSizeZ;
    fmi2Real pickupArmX;
    fmi2Real pickupArmY;
    fmi2Real pickupArmZ;
    fmi2Real pickupArmAngle;
    fmi2Real pickupArmReach;
    fmi2Real pickupDropY;
    fmi2Real pickupArmReachProgress;
    fmi2Real pickupDropProgress;
    fmi2Real pickupGripperGap;
    fmi2Integer ordersPicked;
    fmi2Integer orderQuantity;
    fmi2Integer truckLoadCount;
    fmi2Integer phase;
    fmi2Real phaseTime;
    fmi2Boolean running;
    fmi2Boolean jam;
    fmi2Boolean rushOrder;
    fmi2Boolean rushOrderInputActive;
    fmi2Boolean rushOrderPending;
    fmi2Boolean packageOnAgv;
    fmi2Boolean alarm;
    fmi2Boolean orderComplete;
    fmi2Boolean sourcePackageVisible;
    fmi2Boolean pickupPackageVisible;
    fmi2Boolean transferPackageVisible;
    fmi2Boolean shelfPackageAvailable[6];
} SmartWarehouseCell;

enum { PHASE_PICKUP = 0, PHASE_OUTBOUND = 1, PHASE_TRANSFER = 2, PHASE_RETURN = 3, PHASE_COMPLETE = 4 };

static const fmi2Real PICKUP_DURATION = 7.2;
static const fmi2Real PICKUP_MOVE_TO_SHELF_END = 1.2;
static const fmi2Real PICKUP_LOWER_END = 1.8;
static const fmi2Real PICKUP_GRIP_END = 2.05;
static const fmi2Real PICKUP_LIFT_END = 2.75;
static const fmi2Real PICKUP_RETURN_END = 4.15;
static const fmi2Real PICKUP_EXTEND_END = 5.10;
static const fmi2Real PICKUP_LOWER_AGV_END = 5.80;
static const fmi2Real PICKUP_HANDOFF_END = 5.95;
static const fmi2Real PICKUP_CLEAR_END = 6.55;
static const fmi2Real PICKUP_HOME_X = -3.5;
static const fmi2Real PICKUP_RAIL_Y = 3.3;
static const fmi2Real PICKUP_CLEARANCE_Y = 3.0;
static const fmi2Real PICKUP_AGV_Y = 0.585;
static const fmi2Real PICKUP_AGV_Z = 0.73;
static const fmi2Real TRANSFER_DURATION = 3.2;
static const fmi2Real ROUTE_LENGTH = 8.4;
static const fmi2Real PACKAGE_SIZES[6][3] = {
    {0.48, 0.42, 0.46}, {0.60, 0.54, 0.40}, {0.42, 0.36, 0.64},
    {0.56, 0.60, 0.50}, {0.60, 0.48, 0.62}, {0.50, 0.56, 0.58}
};
static const fmi2Real SOURCE_PACKAGE_X[6] = {-4.0, -3.5, -3.0, -1.7, -1.2, -0.7};

static fmi2Real clamp(fmi2Real value, fmi2Real minimum, fmi2Real maximum) {
    return fmin(maximum, fmax(minimum, value));
}

static fmi2Real smoothstep(fmi2Real progress) {
    const fmi2Real t = clamp(progress, 0.0, 1.0);
    return t * t * (3.0 - 2.0 * t);
}

static fmi2Real interpolate(fmi2Real start, fmi2Real end, fmi2Real progress) {
    return start + (end - start) * smoothstep(progress);
}

static void update_selected_package(SmartWarehouseCell* state) {
    const int index = (int)clamp(state->truckLoadCount, 0, 5);
    const fmi2Real sourceY = 2.01 + PACKAGE_SIZES[index][1] * 0.5;

    state->packageSizeX = PACKAGE_SIZES[index][0];
    state->packageSizeY = PACKAGE_SIZES[index][1];
    state->packageSizeZ = PACKAGE_SIZES[index][2];
    state->pickupArmY = PICKUP_RAIL_Y;
    state->pickupArmZ = -3.08;
    state->pickupArmAngle = 0.0;
    state->pickupArmReach = PICKUP_AGV_Z - state->pickupArmZ;
    state->pickupDropY = sourceY - state->pickupArmY;
    state->sourcePackageVisible = state->shelfPackageAvailable[index]
        && (state->phase != PHASE_PICKUP || state->phaseTime < PICKUP_LOWER_END);
}

static void update_pickup_arm_animation(SmartWarehouseCell* state) {
    const int index = (int)clamp(state->truckLoadCount, 0, 5);
    const fmi2Real sourceX = SOURCE_PACKAGE_X[index];
    const fmi2Real sourceY = 2.01 + state->packageSizeY * 0.5;
    const fmi2Real sourceDrop = sourceY - PICKUP_RAIL_Y;
    const fmi2Real clearanceDrop = PICKUP_CLEARANCE_Y - PICKUP_RAIL_Y;
    const fmi2Real agvDrop = PICKUP_AGV_Y + state->packageSizeY * 0.5 - PICKUP_RAIL_Y;
    const fmi2Real time = state->phase == PHASE_PICKUP
        ? clamp(state->phaseTime, 0.0, PICKUP_DURATION)
        : PICKUP_DURATION;

    state->pickupArmX = PICKUP_HOME_X;
    state->pickupArmReachProgress = 0.0;
    state->pickupDropProgress = clearanceDrop;
    state->pickupGripperGap = 1.0;
    state->robotAngle = 0.12;

    if (state->phase != PHASE_PICKUP) return;

    if (time < PICKUP_MOVE_TO_SHELF_END) {
        state->pickupArmX = interpolate(PICKUP_HOME_X, sourceX, time / PICKUP_MOVE_TO_SHELF_END);
    } else if (time < PICKUP_LOWER_END) {
        state->pickupArmX = sourceX;
        state->pickupDropProgress = interpolate(
            clearanceDrop,
            sourceDrop,
            (time - PICKUP_MOVE_TO_SHELF_END) / (PICKUP_LOWER_END - PICKUP_MOVE_TO_SHELF_END));
    } else if (time < PICKUP_GRIP_END) {
        state->pickupArmX = sourceX;
        state->pickupDropProgress = sourceDrop;
        state->pickupGripperGap = interpolate(
            1.0,
            state->packageSizeX + 0.08,
            (time - PICKUP_LOWER_END) / (PICKUP_GRIP_END - PICKUP_LOWER_END));
        state->robotAngle = interpolate(0.12, 0.34,
            (time - PICKUP_LOWER_END) / (PICKUP_GRIP_END - PICKUP_LOWER_END));
    } else if (time < PICKUP_LIFT_END) {
        state->pickupArmX = sourceX;
        state->pickupDropProgress = interpolate(
            sourceDrop,
            clearanceDrop,
            (time - PICKUP_GRIP_END) / (PICKUP_LIFT_END - PICKUP_GRIP_END));
        state->pickupGripperGap = state->packageSizeX + 0.08;
        state->robotAngle = interpolate(0.34, 0.12,
            (time - PICKUP_GRIP_END) / (PICKUP_LIFT_END - PICKUP_GRIP_END));
    } else if (time < PICKUP_RETURN_END) {
        state->pickupArmX = interpolate(
            sourceX,
            PICKUP_HOME_X,
            (time - PICKUP_LIFT_END) / (PICKUP_RETURN_END - PICKUP_LIFT_END));
        state->pickupGripperGap = state->packageSizeX + 0.08;
    } else if (time < PICKUP_EXTEND_END) {
        state->pickupArmReachProgress = interpolate(
            0.0,
            state->pickupArmReach,
            (time - PICKUP_RETURN_END) / (PICKUP_EXTEND_END - PICKUP_RETURN_END));
        state->pickupGripperGap = state->packageSizeX + 0.08;
    } else if (time < PICKUP_LOWER_AGV_END) {
        state->pickupArmReachProgress = state->pickupArmReach;
        state->pickupDropProgress = interpolate(
            clearanceDrop,
            agvDrop,
            (time - PICKUP_EXTEND_END) / (PICKUP_LOWER_AGV_END - PICKUP_EXTEND_END));
        state->pickupGripperGap = state->packageSizeX + 0.08;
    } else if (time < PICKUP_HANDOFF_END) {
        state->pickupArmReachProgress = state->pickupArmReach;
        state->pickupDropProgress = agvDrop;
        state->pickupGripperGap = state->packageSizeX + 0.08;
    } else if (time < PICKUP_CLEAR_END) {
        state->pickupArmReachProgress = state->pickupArmReach;
        state->pickupDropProgress = interpolate(
            agvDrop,
            clearanceDrop,
            (time - PICKUP_HANDOFF_END) / (PICKUP_CLEAR_END - PICKUP_HANDOFF_END));
        state->pickupGripperGap = interpolate(
            state->packageSizeX + 0.08,
            1.0,
            (time - PICKUP_HANDOFF_END) / (PICKUP_CLEAR_END - PICKUP_HANDOFF_END));
    } else {
        state->pickupArmReachProgress = interpolate(
            state->pickupArmReach,
            0.0,
            (time - PICKUP_CLEAR_END) / (PICKUP_DURATION - PICKUP_CLEAR_END));
    }
}

static void initialize_state(SmartWarehouseCell* state) {
    state->speed = 0.68;
    state->running = 1;
    state->inventoryLevel = 100.0;
    state->orderQuantity = 6;
    state->phase = PHASE_PICKUP;
    state->sourcePackageVisible = 1;
    for (int index = 0; index < 6; ++index) state->shelfPackageAvailable[index] = 1;
    state->transferPackageX = 4.9;
    state->transferPackageY = 0.73 + PACKAGE_SIZES[0][1] * 0.5;
    state->transferPackageZ = 0.73;
    state->robotAngle = 0.12;
    state->forkHeight = 0.38;
    update_selected_package(state);
    update_pickup_arm_animation(state);
}

#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

static SmartWarehouseCell* cell(fmi2Component component) { return (SmartWarehouseCell*)component; }

EXPORT fmi2String fmi2GetTypesPlatform(void) { return "default"; }
EXPORT fmi2String fmi2GetVersion(void) { return "2.0"; }
EXPORT fmi2Status fmi2SetDebugLogging(fmi2Component c, fmi2Boolean loggingOn, size_t nCategories, const fmi2String categories[]) {
    (void)c; (void)loggingOn; (void)nCategories; (void)categories; return fmi2OK;
}
EXPORT fmi2Component fmi2Instantiate(fmi2String instanceName, fmi2Type fmuType, fmi2String guid, fmi2String resourceLocation,
                                      const fmi2CallbackFunctions* callbacks, fmi2Boolean visible, fmi2Boolean loggingOn) {
    (void)instanceName; (void)resourceLocation; (void)visible; (void)loggingOn;
    if (fmuType != fmi2CoSimulation || !guid || strcmp(guid, "{3a923a08-a064-4c43-a4f5-4f297fa589de}") != 0 || !callbacks) return NULL;
    SmartWarehouseCell* state = (SmartWarehouseCell*)calloc(1, sizeof(SmartWarehouseCell));
    if (!state) return NULL;
    state->callbacks = *callbacks;
    initialize_state(state);
    return state;
}
EXPORT void fmi2FreeInstance(fmi2Component c) { free(c); }
EXPORT fmi2Status fmi2SetupExperiment(fmi2Component c, fmi2Boolean toleranceDefined, fmi2Real tolerance,
                                      fmi2Real startTime, fmi2Boolean stopTimeDefined, fmi2Real stopTime) {
    (void)toleranceDefined; (void)tolerance; (void)stopTimeDefined; (void)stopTime;
    SmartWarehouseCell* s = cell(c); if (!s) return fmi2Error; s->time = startTime; return fmi2OK;
}
EXPORT fmi2Status fmi2EnterInitializationMode(fmi2Component c) { return c ? fmi2OK : fmi2Error; }
EXPORT fmi2Status fmi2ExitInitializationMode(fmi2Component c) { return c ? fmi2OK : fmi2Error; }
EXPORT fmi2Status fmi2Terminate(fmi2Component c) { return c ? fmi2OK : fmi2Error; }
EXPORT fmi2Status fmi2Reset(fmi2Component c) {
    if (!c) return fmi2Error;
    fmi2CallbackFunctions callbacks = cell(c)->callbacks;
    memset(c, 0, sizeof(SmartWarehouseCell));
    cell(c)->callbacks = callbacks;
    initialize_state(cell(c));
    return fmi2OK;
}
EXPORT fmi2Status fmi2DoStep(fmi2Component c, fmi2Real currentCommunicationPoint,
                             fmi2Real communicationStepSize, fmi2Boolean noSetFMUStatePriorToCurrentPoint) {
    (void)noSetFMUStatePriorToCurrentPoint;
    SmartWarehouseCell* s = cell(c);
    if (!s || !isfinite(currentCommunicationPoint) || !isfinite(communicationStepSize) || communicationStepSize <= 0) return fmi2Error;
    s->time = currentCommunicationPoint + communicationStepSize;
    s->alarm = s->jam;
    if (s->rushOrder && !s->rushOrderInputActive) s->rushOrderPending = 1;
    s->rushOrderInputActive = s->rushOrder;
    update_selected_package(s);
    if (s->running && !s->jam) {
        s->phaseTime += communicationStepSize;
        switch (s->phase) {
            case PHASE_PICKUP: {
                s->pickupProgress = clamp(s->phaseTime / PICKUP_DURATION, 0.0, 1.0);
                const int selectedPackage = (int)clamp(s->truckLoadCount, 0, 5);
                if (s->phaseTime >= PICKUP_LOWER_END) s->shelfPackageAvailable[selectedPackage] = 0;
                s->sourcePackageVisible = s->shelfPackageAvailable[selectedPackage]
                    && s->phaseTime < PICKUP_LOWER_END;
                s->pickupPackageVisible = s->phaseTime >= PICKUP_LOWER_END
                    && s->phaseTime < PICKUP_HANDOFF_END;
                s->packageOnAgv = s->phaseTime >= PICKUP_HANDOFF_END;
                s->forkHeight = s->phaseTime < PICKUP_HANDOFF_END ? 0.38 : 0.46;
                if (s->phaseTime >= PICKUP_DURATION) {
                    s->phase = PHASE_OUTBOUND;
                    s->phaseTime = 0.0;
                    s->sourcePackageVisible = 0;
                    s->pickupPackageVisible = 0;
                    s->pickupProgress = 0.0;
                    s->packageOnAgv = 1;
                    s->forkHeight = 0.46;
                }
                break;
            }
            case PHASE_OUTBOUND: {
                const fmi2Real speed = s->speed * (s->rushOrderPending ? 1.45 : 1.0);
                s->agvPosition = fmin(ROUTE_LENGTH, s->agvPosition + speed * communicationStepSize);
                s->robotAngle = 0.12;
                s->forkHeight = 0.46;
                if (s->agvPosition >= ROUTE_LENGTH) {
                    s->agvPosition = ROUTE_LENGTH;
                    s->phase = PHASE_TRANSFER;
                    s->phaseTime = 0.0;
                    s->packageOnAgv = 0;
                    s->transferPackageVisible = 1;
                    s->transferPackageX = 4.9;
                    s->transferPackageY = 0.73 + s->packageSizeY * 0.5;
                    s->transferPackageZ = 0.73;
                    s->loaderReach = 0.0;
                    s->rushOrderPending = 0;
                }
                break;
            }
            case PHASE_TRANSFER: {
                const fmi2Real progress = clamp(s->phaseTime / TRANSFER_DURATION, 0.0, 1.0);
                const fmi2Real beltProgress = clamp(progress / 0.75, 0.0, 1.0);
                const fmi2Real loaderProgress = clamp((progress - 0.75) / 0.25, 0.0, 1.0);
                static const fmi2Real slotX[] = {0.0, -0.7, 0.7, 0.0, -0.7, 0.7};
                static const fmi2Real slotZ[] = {7.27, 7.27, 7.27, 6.27, 6.27, 6.27};
                const int slot = (int)clamp(s->truckLoadCount, 0, 5);
                const fmi2Real targetX = 4.9 + slotX[slot];
                const fmi2Real targetZ = slotZ[slot];
                const fmi2Real beltY = 0.73 + s->packageSizeY * 0.5;
                const fmi2Real cargoY = 0.85 + s->packageSizeY * 0.5;
                s->transferPackageVisible = 1;
                s->transferPackageX = 4.9 + (targetX - 4.9) * loaderProgress;
                s->transferPackageY = beltY + (cargoY - beltY) * loaderProgress
                    + 0.12 * sin(3.141592653589793 * loaderProgress);
                s->transferPackageZ = 0.73 + 4.72 * beltProgress + (targetZ - 5.45) * loaderProgress;
                const fmi2Real loaderOffsetX = s->transferPackageX - 4.9;
                const fmi2Real loaderOffsetZ = s->transferPackageZ - 5.12;
                s->loaderAngle = loaderOffsetZ > 0.0 ? atan2(loaderOffsetX, loaderOffsetZ) : 0.0;
                s->loaderReach = loaderOffsetZ > 0.0
                    ? sqrt(loaderOffsetX * loaderOffsetX + loaderOffsetZ * loaderOffsetZ)
                    : 0.0;
                s->beltPhase = fmod(s->beltPhase + communicationStepSize * (2.0 + 4.0 * s->speed),
                                    6.283185307179586);
                if (s->phaseTime >= TRANSFER_DURATION) {
                    s->truckLoadCount++;
                    s->inventoryLevel = fmax(0.0, 100.0 - 4.0 * s->truckLoadCount);
                    s->transferPackageVisible = 0;
                    s->loaderAngle = 0.0;
                    s->loaderReach = 0.0;
                    s->pickupProgress = 0.0;
                    if (s->truckLoadCount >= s->orderQuantity || s->truckLoadCount >= 6) {
                        s->ordersPicked++;
                        s->orderComplete = 1;
                        s->phase = PHASE_COMPLETE;
                    } else {
                        s->phase = PHASE_RETURN;
                    }
                    s->phaseTime = 0.0;
                }
                break;
            }
            case PHASE_RETURN: {
                s->agvPosition = fmax(0.0, s->agvPosition - s->speed * communicationStepSize);
                s->robotAngle = 0.12;
                if (s->agvPosition <= 0.0) {
                    s->agvPosition = 0.0;
                    s->phase = PHASE_PICKUP;
                    s->phaseTime = 0.0;
                    s->pickupProgress = 0.0;
                    s->sourcePackageVisible = s->shelfPackageAvailable[(int)clamp(s->truckLoadCount, 0, 5)];
                    s->pickupPackageVisible = 0;
                }
                break;
            }
            default:
                s->phase = PHASE_COMPLETE;
                s->agvPosition = ROUTE_LENGTH;
                s->packageOnAgv = 0;
                s->sourcePackageVisible = 0;
                s->pickupPackageVisible = 0;
                s->transferPackageVisible = 0;
                s->robotAngle = 0.12;
                s->forkHeight = 0.46;
                break;
        }
    }
    update_selected_package(s);
    update_pickup_arm_animation(s);
    return fmi2OK;
}
EXPORT fmi2Status fmi2GetReal(fmi2Component c, const fmi2ValueReference vr[], size_t n, fmi2Real values[]) {
    SmartWarehouseCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        switch (vr[i]) {
            case 0: values[i] = s->speed; break;
            case 4: values[i] = s->agvPosition; break;
            case 5: values[i] = s->robotAngle; break;
            case 10: values[i] = s->forkHeight; break;
            case 11: values[i] = s->inventoryLevel; break;
            case 18: values[i] = s->pickupProgress; break;
            case 19: values[i] = s->transferPackageX; break;
            case 20: values[i] = s->transferPackageY; break;
            case 21: values[i] = s->transferPackageZ; break;
            case 22: values[i] = s->loaderAngle; break;
            case 23: values[i] = s->beltPhase; break;
            case 24: values[i] = s->loaderReach; break;
            case 31: values[i] = s->packageSizeX; break;
            case 32: values[i] = s->packageSizeY; break;
            case 33: values[i] = s->packageSizeZ; break;
            case 34: values[i] = s->pickupArmX; break;
            case 35: values[i] = s->pickupArmY; break;
            case 36: values[i] = s->pickupArmZ; break;
            case 37: values[i] = s->pickupArmAngle; break;
            case 38: values[i] = s->pickupArmReach; break;
            case 39: values[i] = s->pickupDropY; break;
            case 40: values[i] = s->pickupArmReachProgress; break;
            case 41: values[i] = s->pickupDropProgress; break;
            case 45: values[i] = s->pickupGripperGap; break;
            default: return fmi2Error;
        }
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2SetReal(fmi2Component c, const fmi2ValueReference vr[], size_t n, const fmi2Real values[]) {
    SmartWarehouseCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        if (vr[i] != 0 || !isfinite(values[i]) || values[i] < 0 || values[i] > 1.4) return fmi2Error;
        s->speed = values[i];
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2GetInteger(fmi2Component c, const fmi2ValueReference vr[], size_t n, fmi2Integer values[]) {
    SmartWarehouseCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        if (vr[i] == 8) values[i] = s->ordersPicked;
        else if (vr[i] == 13) values[i] = s->truckLoadCount;
        else return fmi2Error;
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2SetInteger(fmi2Component c, const fmi2ValueReference vr[], size_t n, const fmi2Integer values[]) {
    SmartWarehouseCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        if (vr[i] != 12 || values[i] < 1 || values[i] > 6) return fmi2Error;
        s->orderQuantity = values[i];
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2GetBoolean(fmi2Component c, const fmi2ValueReference vr[], size_t n, fmi2Boolean values[]) {
    SmartWarehouseCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        switch (vr[i]) {
            case 1: values[i] = s->running; break;
            case 2: values[i] = s->jam; break;
            case 3: values[i] = s->rushOrder; break;
            case 6: values[i] = s->packageOnAgv; break;
            case 7: values[i] = s->alarm; break;
            case 14: values[i] = s->orderComplete; break;
            case 15: values[i] = s->sourcePackageVisible; break;
            case 16: values[i] = s->pickupPackageVisible; break;
            case 17: values[i] = s->transferPackageVisible; break;
            case 25: values[i] = s->shelfPackageAvailable[0]; break;
            case 26: values[i] = s->shelfPackageAvailable[1]; break;
            case 27: values[i] = s->shelfPackageAvailable[2]; break;
            case 28: values[i] = s->shelfPackageAvailable[3]; break;
            case 29: values[i] = s->shelfPackageAvailable[4]; break;
            case 30: values[i] = s->shelfPackageAvailable[5]; break;
            case 42: values[i] = ((int)clamp(s->truckLoadCount, 0, 5) % 3) == 0; break;
            case 43: values[i] = ((int)clamp(s->truckLoadCount, 0, 5) % 3) == 1; break;
            case 44: values[i] = ((int)clamp(s->truckLoadCount, 0, 5) % 3) == 2; break;
            default: return fmi2Error;
        }
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2SetBoolean(fmi2Component c, const fmi2ValueReference vr[], size_t n, const fmi2Boolean values[]) {
    SmartWarehouseCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        if (vr[i] == 1) s->running = values[i] != 0;
        else if (vr[i] == 2) s->jam = values[i] != 0;
        else if (vr[i] == 3) s->rushOrder = values[i] != 0;
        else return fmi2Error;
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2GetString(fmi2Component c, const fmi2ValueReference vr[], size_t n, fmi2String values[]) {
    (void)c; (void)vr; (void)n; (void)values; return fmi2Error;
}
EXPORT fmi2Status fmi2SetString(fmi2Component c, const fmi2ValueReference vr[], size_t n, const fmi2String values[]) {
    (void)c; (void)vr; (void)n; (void)values; return fmi2Error;
}

/* FMI 2.0 Co-Simulation API symbols unused by this deterministic demo. */
EXPORT fmi2Status fmi2CancelStep(fmi2Component c) { (void)c; return fmi2Error; }
EXPORT fmi2Status fmi2GetFMUstate(fmi2Component c, fmi2FMUstate* state) { (void)c; (void)state; return fmi2Error; }
EXPORT fmi2Status fmi2SetFMUstate(fmi2Component c, fmi2FMUstate state) { (void)c; (void)state; return fmi2Error; }
EXPORT fmi2Status fmi2FreeFMUstate(fmi2Component c, fmi2FMUstate* state) { (void)c; (void)state; return fmi2Error; }
EXPORT fmi2Status fmi2SerializedFMUstateSize(fmi2Component c, fmi2FMUstate state, size_t* size) { (void)c; (void)state; (void)size; return fmi2Error; }
EXPORT fmi2Status fmi2SerializeFMUstate(fmi2Component c, fmi2FMUstate state, fmi2Byte serializedState[], size_t size) { (void)c; (void)state; (void)serializedState; (void)size; return fmi2Error; }
EXPORT fmi2Status fmi2DeSerializeFMUstate(fmi2Component c, const fmi2Byte serializedState[], size_t size, fmi2FMUstate* state) { (void)c; (void)serializedState; (void)size; (void)state; return fmi2Error; }
EXPORT fmi2Status fmi2GetDirectionalDerivative(fmi2Component c, const fmi2ValueReference unknowns[], size_t unknownCount, const fmi2ValueReference knowns[], size_t knownCount, const fmi2Real knownDerivatives[], fmi2Real unknownDerivatives[]) { (void)c; (void)unknowns; (void)unknownCount; (void)knowns; (void)knownCount; (void)knownDerivatives; (void)unknownDerivatives; return fmi2Error; }
EXPORT fmi2Status fmi2SetRealInputDerivatives(fmi2Component c, const fmi2ValueReference vr[], size_t n, const fmi2Integer order[], const fmi2Real value[]) { (void)c; (void)vr; (void)n; (void)order; (void)value; return fmi2Error; }
EXPORT fmi2Status fmi2GetRealOutputDerivatives(fmi2Component c, const fmi2ValueReference vr[], size_t n, const fmi2Integer order[], fmi2Real value[]) { (void)c; (void)vr; (void)n; (void)order; (void)value; return fmi2Error; }
EXPORT fmi2Status fmi2GetStatus(fmi2Component c, fmi2StatusKind kind, fmi2Status* value) { (void)c; (void)kind; (void)value; return fmi2Error; }
EXPORT fmi2Status fmi2GetRealStatus(fmi2Component c, fmi2StatusKind kind, fmi2Real* value) { (void)c; (void)kind; (void)value; return fmi2Error; }
EXPORT fmi2Status fmi2GetIntegerStatus(fmi2Component c, fmi2StatusKind kind, fmi2Integer* value) { (void)c; (void)kind; (void)value; return fmi2Error; }
EXPORT fmi2Status fmi2GetBooleanStatus(fmi2Component c, fmi2StatusKind kind, fmi2Boolean* value) { (void)c; (void)kind; (void)value; return fmi2Error; }
EXPORT fmi2Status fmi2GetStringStatus(fmi2Component c, fmi2StatusKind kind, fmi2String* value) { (void)c; (void)kind; (void)value; return fmi2Error; }
