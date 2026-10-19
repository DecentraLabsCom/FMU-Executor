#include <math.h>
#include <stdlib.h>
#include <string.h>

typedef void* fmi2Component;
typedef void* fmi2Environment;
typedef const char* fmi2String;
typedef double fmi2Real;
typedef int fmi2Integer;
typedef int fmi2Boolean;
typedef unsigned int fmi2ValueReference;
typedef enum { fmi2OK = 0, fmi2Warning = 1, fmi2Discard = 2, fmi2Error = 3, fmi2Fatal = 4, fmi2Pending = 5 } fmi2Status;
typedef enum { fmi2ModelExchange = 0, fmi2CoSimulation = 1 } fmi2Type;
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
    fmi2Real position;
    fmi2Real robotAngle;
    fmi2Real sortOffset;
    fmi2Real eventTimer;
    fmi2Integer goodParts;
    fmi2Integer rejectedParts;
    fmi2Boolean running;
    fmi2Boolean jam;
    fmi2Boolean rejectNext;
    fmi2Boolean productPresent;
    fmi2Boolean alarm;
} SmartFactoryCell;

#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

static SmartFactoryCell* cell(fmi2Component component) { return (SmartFactoryCell*)component; }

EXPORT fmi2String fmi2GetTypesPlatform(void) { return "default"; }
EXPORT fmi2String fmi2GetVersion(void) { return "2.0"; }
EXPORT fmi2Status fmi2SetDebugLogging(fmi2Component c, fmi2Boolean loggingOn, size_t nCategories, const fmi2String categories[]) {
    (void)c; (void)loggingOn; (void)nCategories; (void)categories; return fmi2OK;
}
EXPORT fmi2Component fmi2Instantiate(fmi2String instanceName, fmi2Type fmuType, fmi2String guid, fmi2String resourceLocation,
                                      const fmi2CallbackFunctions* callbacks, fmi2Boolean visible, fmi2Boolean loggingOn) {
    (void)instanceName; (void)resourceLocation; (void)visible; (void)loggingOn;
    if (fmuType != fmi2CoSimulation || !guid || strcmp(guid, "{f85cda84-9872-4aaf-a9f3-02b7a31142a1}") != 0 || !callbacks) return NULL;
    SmartFactoryCell* state = (SmartFactoryCell*)calloc(1, sizeof(SmartFactoryCell));
    if (!state) return NULL;
    state->callbacks = *callbacks;
    state->speed = 0.45;
    state->running = 1;
    state->productPresent = 1;
    return state;
}
EXPORT void fmi2FreeInstance(fmi2Component c) { free(c); }
EXPORT fmi2Status fmi2SetupExperiment(fmi2Component c, fmi2Boolean toleranceDefined, fmi2Real tolerance,
                                      fmi2Real startTime, fmi2Boolean stopTimeDefined, fmi2Real stopTime) {
    (void)toleranceDefined; (void)tolerance; (void)stopTimeDefined; (void)stopTime;
    SmartFactoryCell* s = cell(c); if (!s) return fmi2Error; s->time = startTime; return fmi2OK;
}
EXPORT fmi2Status fmi2EnterInitializationMode(fmi2Component c) { return c ? fmi2OK : fmi2Error; }
EXPORT fmi2Status fmi2ExitInitializationMode(fmi2Component c) { return c ? fmi2OK : fmi2Error; }
EXPORT fmi2Status fmi2Terminate(fmi2Component c) { return c ? fmi2OK : fmi2Error; }
EXPORT fmi2Status fmi2Reset(fmi2Component c) {
    if (!c) return fmi2Error;
    fmi2CallbackFunctions callbacks = cell(c)->callbacks;
    memset(c, 0, sizeof(SmartFactoryCell));
    cell(c)->callbacks = callbacks;
    cell(c)->speed = 0.45; cell(c)->running = 1; cell(c)->productPresent = 1;
    return fmi2OK;
}
EXPORT fmi2Status fmi2DoStep(fmi2Component c, fmi2Real currentCommunicationPoint,
                             fmi2Real communicationStepSize, fmi2Boolean noSetFMUStatePriorToCurrentPoint) {
    (void)noSetFMUStatePriorToCurrentPoint;
    SmartFactoryCell* s = cell(c);
    if (!s || !isfinite(currentCommunicationPoint) || !isfinite(communicationStepSize) || communicationStepSize <= 0) return fmi2Error;
    s->time = currentCommunicationPoint + communicationStepSize;
    s->alarm = s->jam;
    if (s->eventTimer > 0) s->eventTimer = fmax(0, s->eventTimer - communicationStepSize);
    if (s->running && !s->jam) {
        s->position += s->speed * communicationStepSize;
        if (s->position >= 3.0) {
            s->position = fmod(s->position, 3.0);
            if (s->rejectNext) s->rejectedParts++;
            else s->goodParts++;
            s->rejectNext = 0;
            s->eventTimer = 0.35;
        }
    }
    s->sortOffset = 0;
    if (s->position >= 1.45 && s->position <= 2.15) {
        s->robotAngle = -0.95;
        s->sortOffset = s->rejectNext ? -0.62 : 0.62;
    } else if (s->eventTimer > 0) s->robotAngle = -0.62;
    else s->robotAngle = -0.08 + 0.08 * sin(s->time * 1.4);
    return fmi2OK;
}
EXPORT fmi2Status fmi2GetReal(fmi2Component c, const fmi2ValueReference vr[], size_t n, fmi2Real values[]) {
    SmartFactoryCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        switch (vr[i]) {
            case 0: values[i] = s->speed; break;
            case 4: values[i] = s->position; break;
            case 5: values[i] = s->robotAngle; break;
            case 10: values[i] = s->sortOffset; break;
            default: return fmi2Error;
        }
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2SetReal(fmi2Component c, const fmi2ValueReference vr[], size_t n, const fmi2Real values[]) {
    SmartFactoryCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        if (vr[i] != 0 || !isfinite(values[i]) || values[i] < 0 || values[i] > 1.2) return fmi2Error;
        s->speed = values[i];
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2GetInteger(fmi2Component c, const fmi2ValueReference vr[], size_t n, fmi2Integer values[]) {
    SmartFactoryCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        if (vr[i] == 8) values[i] = s->goodParts;
        else if (vr[i] == 9) values[i] = s->rejectedParts;
        else return fmi2Error;
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2SetInteger(fmi2Component c, const fmi2ValueReference vr[], size_t n, const fmi2Integer values[]) {
    (void)c; (void)vr; (void)n; (void)values; return fmi2Error;
}
EXPORT fmi2Status fmi2GetBoolean(fmi2Component c, const fmi2ValueReference vr[], size_t n, fmi2Boolean values[]) {
    SmartFactoryCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        switch (vr[i]) {
            case 1: values[i] = s->running; break;
            case 2: values[i] = s->jam; break;
            case 3: values[i] = s->rejectNext; break;
            case 6: values[i] = s->productPresent; break;
            case 7: values[i] = s->alarm; break;
            default: return fmi2Error;
        }
    }
    return fmi2OK;
}
EXPORT fmi2Status fmi2SetBoolean(fmi2Component c, const fmi2ValueReference vr[], size_t n, const fmi2Boolean values[]) {
    SmartFactoryCell* s = cell(c); if (!s || (!vr && n) || (!values && n)) return fmi2Error;
    for (size_t i = 0; i < n; ++i) {
        if (vr[i] == 1) s->running = values[i] != 0;
        else if (vr[i] == 2) s->jam = values[i] != 0;
        else if (vr[i] == 3) s->rejectNext = values[i] != 0;
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
