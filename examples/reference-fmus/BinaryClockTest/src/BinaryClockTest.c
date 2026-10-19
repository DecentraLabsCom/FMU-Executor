/*
 * BinaryClockTest.c — Minimal FMI 3.0 Co-Simulation model for Binary & Clock
 *                      variable type validation.
 *
 * Variables:
 *   vr=0  dataIn     Binary  input   — arbitrary byte payload
 *   vr=1  dataOut    Binary  output  — echoes dataIn with a 1-byte length prefix
 *   vr=2  heartbeat  Clock   output  — ticks active every 10 doStep calls
 *   vr=3  stepCount  Float64 output  — counts simulation steps
 *
 * Build:  cl /LD /O2 /DWIN32 BinaryClockTest.c /Fe:BinaryClockTest.dll
 */

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* FMI 3.0 type definitions (self-contained, no external header)      */
/* ------------------------------------------------------------------ */

#if defined(_WIN32)
#  define EXPORT __declspec(dllexport)
#else
#  define EXPORT __attribute__((visibility("default")))
#endif

typedef void*       fmi3Instance;
typedef void*       fmi3InstanceEnvironment;
typedef uint32_t    fmi3ValueReference;
typedef double      fmi3Float64;
typedef int         fmi3Boolean;
typedef int         fmi3Status;
typedef uint8_t     fmi3Byte;
typedef const fmi3Byte* fmi3Binary;
typedef fmi3Boolean fmi3Clock;
typedef const char* fmi3String;

enum { fmi3False = 0, fmi3True = 1 };
enum { fmi3OK = 0, fmi3Warning = 1, fmi3Discard = 2, fmi3Error = 3, fmi3Fatal = 4 };

typedef void (*fmi3LogMessageCallback)(fmi3InstanceEnvironment, fmi3Status, fmi3String, fmi3String);
typedef void (*fmi3IntermediateUpdateCallback)(fmi3InstanceEnvironment, fmi3Float64,
    fmi3Boolean, fmi3Boolean, fmi3Boolean, fmi3Boolean, fmi3Boolean*, fmi3Float64*);

/* ------------------------------------------------------------------ */
/* Instance state                                                     */
/* ------------------------------------------------------------------ */

#define HEARTBEAT_PERIOD 10
#define MAX_BIN_SIZE     4096

typedef struct {
    /* Binary input  vr=0 */
    uint8_t  data_in[MAX_BIN_SIZE];
    size_t   data_in_size;

    /* Binary output vr=1 (length-prefixed echo) */
    uint8_t  data_out[MAX_BIN_SIZE + 1];
    size_t   data_out_size;

    /* Clock output  vr=2 */
    fmi3Boolean heartbeat;

    /* Float64 output vr=3 */
    double   step_count;

    /* Internal */
    int      step_counter;
} ModelInstance;

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

static void update_outputs(ModelInstance* m) {
    /* dataOut = [len_lo] ++ dataIn  (1-byte length prefix, mod 256) */
    m->data_out[0] = (uint8_t)(m->data_in_size & 0xFF);
    if (m->data_in_size > 0)
        memcpy(m->data_out + 1, m->data_in, m->data_in_size);
    m->data_out_size = 1 + m->data_in_size;

    /* heartbeat ticks every HEARTBEAT_PERIOD steps */
    m->heartbeat = (m->step_counter % HEARTBEAT_PERIOD == 0) ? fmi3True : fmi3False;

    m->step_count = (double)m->step_counter;
}

/* ------------------------------------------------------------------ */
/* FMI 3.0 required functions                                         */
/* ------------------------------------------------------------------ */

EXPORT const char* fmi3GetVersion(void) { return "3.0"; }

EXPORT fmi3Status fmi3SetDebugLogging(fmi3Instance inst, fmi3Boolean on,
    size_t nCat, const fmi3String cats[])
{
    (void)inst; (void)on; (void)nCat; (void)cats;
    return fmi3OK;
}

EXPORT fmi3Instance fmi3InstantiateCoSimulation(
    fmi3String instanceName,
    fmi3String instantiationToken,
    fmi3String resourcePath,
    fmi3Boolean visible,
    fmi3Boolean loggingOn,
    fmi3Boolean eventModeUsed,
    fmi3Boolean earlyReturnAllowed,
    const fmi3ValueReference requiredIntermediateVariables[],
    size_t nRequiredIntermediateVariables,
    fmi3InstanceEnvironment instanceEnvironment,
    fmi3LogMessageCallback logMessage,
    fmi3IntermediateUpdateCallback intermediateUpdate)
{
    (void)instanceName; (void)instantiationToken; (void)resourcePath;
    (void)visible; (void)loggingOn; (void)eventModeUsed;
    (void)earlyReturnAllowed; (void)requiredIntermediateVariables;
    (void)nRequiredIntermediateVariables; (void)instanceEnvironment;
    (void)logMessage; (void)intermediateUpdate;

    ModelInstance* m = (ModelInstance*)calloc(1, sizeof(ModelInstance));
    if (!m) return NULL;

    /* Default dataIn: 3 bytes {0x01, 0x02, 0x03} matching modelDescription start */
    m->data_in[0] = 0x01; m->data_in[1] = 0x02; m->data_in[2] = 0x03;
    m->data_in_size = 3;

    update_outputs(m);
    return (fmi3Instance)m;
}

EXPORT fmi3Instance fmi3InstantiateModelExchange(
    fmi3String a, fmi3String b, fmi3String c,
    fmi3Boolean d, fmi3Boolean e,
    fmi3InstanceEnvironment f, fmi3LogMessageCallback g)
{
    (void)a;(void)b;(void)c;(void)d;(void)e;(void)f;(void)g;
    return NULL; /* not supported */
}

EXPORT void fmi3FreeInstance(fmi3Instance inst) {
    free(inst);
}

EXPORT fmi3Status fmi3EnterInitializationMode(
    fmi3Instance inst, fmi3Boolean tolDef, fmi3Float64 tol,
    fmi3Float64 startTime, fmi3Boolean stopDef, fmi3Float64 stopTime)
{
    (void)inst;(void)tolDef;(void)tol;(void)startTime;(void)stopDef;(void)stopTime;
    return fmi3OK;
}

EXPORT fmi3Status fmi3ExitInitializationMode(fmi3Instance inst) {
    (void)inst;
    return fmi3OK;
}

EXPORT fmi3Status fmi3EnterEventMode(fmi3Instance inst) {
    (void)inst;
    return fmi3OK;
}

EXPORT fmi3Status fmi3Terminate(fmi3Instance inst) {
    (void)inst;
    return fmi3OK;
}

EXPORT fmi3Status fmi3Reset(fmi3Instance inst) {
    ModelInstance* m = (ModelInstance*)inst;
    if (!m) return fmi3Error;
    memset(m, 0, sizeof(ModelInstance));
    m->data_in[0] = 0x01; m->data_in[1] = 0x02; m->data_in[2] = 0x03;
    m->data_in_size = 3;
    update_outputs(m);
    return fmi3OK;
}

/* ------------------------------------------------------------------ */
/* DoStep                                                             */
/* ------------------------------------------------------------------ */

EXPORT fmi3Status fmi3DoStep(
    fmi3Instance inst,
    fmi3Float64 currentCommunicationPoint,
    fmi3Float64 communicationStepSize,
    fmi3Boolean noSetFMUStatePriorToCurrentPoint,
    fmi3Boolean* eventHandlingNeeded,
    fmi3Boolean* terminateSimulation,
    fmi3Boolean* earlyReturn,
    fmi3Float64* lastSuccessfulTime)
{
    ModelInstance* m = (ModelInstance*)inst;
    if (!m) return fmi3Error;

    (void)currentCommunicationPoint;
    (void)communicationStepSize;
    (void)noSetFMUStatePriorToCurrentPoint;

    m->step_counter++;
    update_outputs(m);

    if (eventHandlingNeeded) *eventHandlingNeeded = fmi3False;
    if (terminateSimulation)  *terminateSimulation  = fmi3False;
    if (earlyReturn)          *earlyReturn          = fmi3False;
    if (lastSuccessfulTime)   *lastSuccessfulTime   = currentCommunicationPoint + communicationStepSize;
    return fmi3OK;
}

/* ------------------------------------------------------------------ */
/* Getters                                                            */
/* ------------------------------------------------------------------ */

EXPORT fmi3Status fmi3GetFloat64(
    fmi3Instance inst,
    const fmi3ValueReference vr[], size_t nvr,
    fmi3Float64 values[], size_t nv)
{
    ModelInstance* m = (ModelInstance*)inst;
    if (!m) return fmi3Error;
    (void)nv;
    for (size_t i = 0; i < nvr; i++) {
        if (vr[i] == 3) values[i] = m->step_count;
        else return fmi3Error;
    }
    return fmi3OK;
}

EXPORT fmi3Status fmi3GetBinary(
    fmi3Instance inst,
    const fmi3ValueReference vr[], size_t nvr,
    size_t valueSizes[], fmi3Binary values[], size_t nv)
{
    ModelInstance* m = (ModelInstance*)inst;
    if (!m) return fmi3Error;
    (void)nv;
    for (size_t i = 0; i < nvr; i++) {
        switch (vr[i]) {
        case 0: /* dataIn */
            valueSizes[i] = m->data_in_size;
            values[i]     = m->data_in;
            break;
        case 1: /* dataOut */
            valueSizes[i] = m->data_out_size;
            values[i]     = m->data_out;
            break;
        default:
            return fmi3Error;
        }
    }
    return fmi3OK;
}

EXPORT fmi3Status fmi3GetClock(
    fmi3Instance inst,
    const fmi3ValueReference vr[], size_t nvr,
    fmi3Clock values[])
{
    ModelInstance* m = (ModelInstance*)inst;
    if (!m) return fmi3Error;
    for (size_t i = 0; i < nvr; i++) {
        if (vr[i] == 2) values[i] = m->heartbeat;
        else return fmi3Error;
    }
    return fmi3OK;
}

/* ------------------------------------------------------------------ */
/* Setters                                                            */
/* ------------------------------------------------------------------ */

EXPORT fmi3Status fmi3SetFloat64(
    fmi3Instance inst,
    const fmi3ValueReference vr[], size_t nvr,
    const fmi3Float64 values[], size_t nv)
{
    (void)inst;(void)vr;(void)nvr;(void)values;(void)nv;
    return fmi3OK; /* no float inputs */
}

EXPORT fmi3Status fmi3SetBinary(
    fmi3Instance inst,
    const fmi3ValueReference vr[], size_t nvr,
    const size_t valueSizes[], const fmi3Binary values[], size_t nv)
{
    ModelInstance* m = (ModelInstance*)inst;
    if (!m) return fmi3Error;
    (void)nv;
    for (size_t i = 0; i < nvr; i++) {
        if (vr[i] == 0) { /* dataIn */
            size_t sz = valueSizes[i];
            if (sz > MAX_BIN_SIZE) sz = MAX_BIN_SIZE;
            memcpy(m->data_in, values[i], sz);
            m->data_in_size = sz;
            update_outputs(m);
        } else {
            return fmi3Error;
        }
    }
    return fmi3OK;
}

EXPORT fmi3Status fmi3SetClock(
    fmi3Instance inst,
    const fmi3ValueReference vr[], size_t nvr,
    const fmi3Clock values[])
{
    (void)inst;(void)vr;(void)nvr;(void)values;
    return fmi3OK; /* heartbeat is output-only */
}

/* ------------------------------------------------------------------ */
/* Stubs for remaining required exports                               */
/* ------------------------------------------------------------------ */

EXPORT fmi3Status fmi3GetFloat32(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, float val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetInt8(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, int8_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetUInt8(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, uint8_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetInt16(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, int16_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetUInt16(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, uint16_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetInt32(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, int32_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetUInt32(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, uint32_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetInt64(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, int64_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetUInt64(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, uint64_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetBoolean(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, int val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }
EXPORT fmi3Status fmi3GetString(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const char* val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3Error; }

EXPORT fmi3Status fmi3SetFloat32(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const float val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetInt8(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const int8_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetUInt8(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const uint8_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetInt16(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const int16_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetUInt16(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const uint16_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetInt32(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const int32_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetUInt32(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const uint32_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetInt64(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const int64_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetUInt64(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const uint64_t val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetBoolean(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const int val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }
EXPORT fmi3Status fmi3SetString(fmi3Instance i, const fmi3ValueReference v[],
    size_t n, const char* const val[], size_t nv) { (void)i;(void)v;(void)n;(void)val;(void)nv; return fmi3OK; }

EXPORT fmi3Status fmi3GetFMUState(fmi3Instance i, void** s) { (void)i;(void)s; return fmi3Error; }
EXPORT fmi3Status fmi3SetFMUState(fmi3Instance i, void* s)  { (void)i;(void)s; return fmi3Error; }
EXPORT fmi3Status fmi3FreeFMUState(fmi3Instance i, void** s){ (void)i;(void)s; return fmi3Error; }
EXPORT fmi3Status fmi3SerializedFMUStateSize(fmi3Instance i, void* s, size_t* sz)
    { (void)i;(void)s;(void)sz; return fmi3Error; }
EXPORT fmi3Status fmi3SerializeFMUState(fmi3Instance i, void* s, uint8_t b[], size_t sz)
    { (void)i;(void)s;(void)b;(void)sz; return fmi3Error; }
EXPORT fmi3Status fmi3DeserializeFMUState(fmi3Instance i, const uint8_t b[], size_t sz, void** s)
    { (void)i;(void)b;(void)sz;(void)s; return fmi3Error; }
EXPORT fmi3Status fmi3GetDirectionalDerivative(
    fmi3Instance i, const uint32_t u[], size_t nu, const uint32_t k[], size_t nk,
    const double seed[], size_t ns, double sens[], size_t nse)
    { (void)i;(void)u;(void)nu;(void)k;(void)nk;(void)seed;(void)ns;(void)sens;(void)nse; return fmi3Error; }
EXPORT fmi3Status fmi3GetAdjointDerivative(
    fmi3Instance i, const uint32_t u[], size_t nu, const uint32_t k[], size_t nk,
    const double seed[], size_t ns, double sens[], size_t nse)
    { (void)i;(void)u;(void)nu;(void)k;(void)nk;(void)seed;(void)ns;(void)sens;(void)nse; return fmi3Error; }

/* ------------------------------------------------------------------ */
/* Additional FMI 3 stubs required by fmpy's DLL loader               */
/* ------------------------------------------------------------------ */

typedef void (*fmi3LockPreemptionCallback)(void);
typedef void (*fmi3UnlockPreemptionCallback)(void);

EXPORT fmi3Instance fmi3InstantiateScheduledExecution(
    fmi3String instanceName, fmi3String instantiationToken,
    fmi3String resourcePath, fmi3Boolean visible, fmi3Boolean loggingOn,
    fmi3InstanceEnvironment instanceEnvironment,
    fmi3LogMessageCallback logMessage,
    fmi3IntermediateUpdateCallback clockUpdate,
    fmi3LockPreemptionCallback lockPreemption,
    fmi3UnlockPreemptionCallback unlockPreemption)
{
    (void)instanceName;(void)instantiationToken;(void)resourcePath;
    (void)visible;(void)loggingOn;(void)instanceEnvironment;
    (void)logMessage;(void)clockUpdate;(void)lockPreemption;(void)unlockPreemption;
    return NULL;
}

EXPORT fmi3Status fmi3ActivateModelPartition(
    fmi3Instance i, fmi3ValueReference clockRef, fmi3Float64 activationTime)
    { (void)i;(void)clockRef;(void)activationTime; return fmi3Error; }

EXPORT fmi3Status fmi3EnterStepMode(fmi3Instance i)
    { (void)i; return fmi3OK; }
EXPORT fmi3Status fmi3EnterConfigurationMode(fmi3Instance i)
    { (void)i; return fmi3OK; }
EXPORT fmi3Status fmi3ExitConfigurationMode(fmi3Instance i)
    { (void)i; return fmi3OK; }
EXPORT fmi3Status fmi3EnterContinuousTimeMode(fmi3Instance i)
    { (void)i; return fmi3Error; }

EXPORT fmi3Status fmi3CompletedIntegratorStep(
    fmi3Instance i, fmi3Boolean noSetFMUStatePrior,
    fmi3Boolean* enterEventMode, fmi3Boolean* terminateSimulation)
    { (void)i;(void)noSetFMUStatePrior;(void)enterEventMode;(void)terminateSimulation; return fmi3Error; }

EXPORT fmi3Status fmi3SetTime(fmi3Instance i, fmi3Float64 time)
    { (void)i;(void)time; return fmi3OK; }
EXPORT fmi3Status fmi3SetContinuousStates(
    fmi3Instance i, const fmi3Float64 x[], size_t nx)
    { (void)i;(void)x;(void)nx; return fmi3Error; }
EXPORT fmi3Status fmi3GetContinuousStateDerivatives(
    fmi3Instance i, fmi3Float64 dx[], size_t ndx)
    { (void)i;(void)dx;(void)ndx; return fmi3Error; }
EXPORT fmi3Status fmi3GetContinuousStates(
    fmi3Instance i, fmi3Float64 x[], size_t nx)
    { (void)i;(void)x;(void)nx; return fmi3Error; }
EXPORT fmi3Status fmi3GetEventIndicators(
    fmi3Instance i, fmi3Float64 ei[], size_t nei)
    { (void)i;(void)ei;(void)nei; return fmi3Error; }
EXPORT fmi3Status fmi3GetNominalsOfContinuousStates(
    fmi3Instance i, fmi3Float64 n[], size_t nn)
    { (void)i;(void)n;(void)nn; return fmi3Error; }
EXPORT fmi3Status fmi3GetNumberOfContinuousStates(
    fmi3Instance i, size_t* ncs)
    { (void)i; if(ncs) *ncs = 0; return fmi3OK; }
EXPORT fmi3Status fmi3GetNumberOfEventIndicators(
    fmi3Instance i, size_t* nei)
    { (void)i; if(nei) *nei = 0; return fmi3OK; }

EXPORT fmi3Status fmi3GetOutputDerivatives(
    fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
    const int32_t orders[], fmi3Float64 values[], size_t nv)
    { (void)i;(void)vr;(void)nvr;(void)orders;(void)values;(void)nv; return fmi3Error; }

EXPORT fmi3Status fmi3EvaluateDiscreteStates(fmi3Instance i)
    { (void)i; return fmi3OK; }
EXPORT fmi3Status fmi3UpdateDiscreteStates(
    fmi3Instance i, fmi3Boolean* discreteStatesNeedUpdate,
    fmi3Boolean* terminateSimulation, fmi3Boolean* nominalsChanged,
    fmi3Boolean* valuesChanged, fmi3Float64* nextEventTime)
{
    (void)i;
    if (discreteStatesNeedUpdate) *discreteStatesNeedUpdate = fmi3False;
    if (terminateSimulation) *terminateSimulation = fmi3False;
    if (nominalsChanged) *nominalsChanged = fmi3False;
    if (valuesChanged) *valuesChanged = fmi3False;
    if (nextEventTime) *nextEventTime = 1e30;
    return fmi3OK;
}

EXPORT fmi3Status fmi3GetIntervalDecimal(
    fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
    fmi3Float64 intervals[], int qualifiers[])
    { (void)i;(void)vr;(void)nvr;(void)intervals;(void)qualifiers; return fmi3Error; }
EXPORT fmi3Status fmi3GetIntervalFraction(
    fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
    uint64_t counters[], uint64_t resolutions[], int qualifiers[])
    { (void)i;(void)vr;(void)nvr;(void)counters;(void)resolutions;(void)qualifiers; return fmi3Error; }
EXPORT fmi3Status fmi3SetIntervalDecimal(
    fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
    const fmi3Float64 intervals[])
    { (void)i;(void)vr;(void)nvr;(void)intervals; return fmi3Error; }
EXPORT fmi3Status fmi3SetIntervalFraction(
    fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
    const uint64_t counters[], const uint64_t resolutions[])
    { (void)i;(void)vr;(void)nvr;(void)counters;(void)resolutions; return fmi3Error; }

EXPORT fmi3Status fmi3GetShiftDecimal(
    fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
    fmi3Float64 shifts[])
    { (void)i;(void)vr;(void)nvr;(void)shifts; return fmi3Error; }
EXPORT fmi3Status fmi3GetShiftFraction(
    fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
    uint64_t counters[], uint64_t resolutions[])
    { (void)i;(void)vr;(void)nvr;(void)counters;(void)resolutions; return fmi3Error; }
EXPORT fmi3Status fmi3SetShiftDecimal(
    fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
    const fmi3Float64 shifts[])
    { (void)i;(void)vr;(void)nvr;(void)shifts; return fmi3Error; }
EXPORT fmi3Status fmi3SetShiftFraction(
    fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
    const uint64_t counters[], const uint64_t resolutions[])
    { (void)i;(void)vr;(void)nvr;(void)counters;(void)resolutions; return fmi3Error; }

EXPORT fmi3Status fmi3GetNumberOfVariableDependencies(
    fmi3Instance i, fmi3ValueReference vr, size_t* nDeps)
    { (void)i;(void)vr; if(nDeps) *nDeps = 0; return fmi3OK; }
EXPORT fmi3Status fmi3GetVariableDependencies(
    fmi3Instance i, fmi3ValueReference dependent,
    size_t elementIndicesOfDependent[],
    fmi3ValueReference independents[],
    size_t elementIndicesOfIndependents[],
    int dependencyKinds[], size_t nDeps)
    { (void)i;(void)dependent;(void)elementIndicesOfDependent;(void)independents;
      (void)elementIndicesOfIndependents;(void)dependencyKinds;(void)nDeps; return fmi3OK; }
