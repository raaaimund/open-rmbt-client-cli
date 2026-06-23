/*
 * rmbt_loop.c — GIL-free recv/send hot loops for RMBT download and upload.
 *
 * Only works on plain TCP sockets (fd passed directly).  TLS and WebSocket
 * connections must use the pure-Python path in tests.py.
 *
 * Exported functions:
 *   download_loop(fd, initial_bytes, chunk_size, read_block, sample_ns)
 *       -> (total_bytes, samples, leftover_bytes)
 *
 *   upload_loop(fd, chunk_size, write_block, duration_ns, sample_ns)
 *       -> (total_bytes, samples)
 *
 * samples is a list of (bytes, time_ns) tuples anchored at (0, 0).
 * The final entry with the server-reported elapsed_ns is added by Python
 * after reading the "TIME <ns>" line.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <sys/socket.h>
#include <poll.h>
#include <errno.h>
#include <time.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

/* Timeout in ms to use when poll()-waiting on a non-blocking fd. */
#define SOCK_TIMEOUT_MS 30000

/*
 * Wait until fd is readable (POLLIN).  Returns 1 on success, 0 on
 * timeout/error.  Called only when recv() returns EAGAIN, which happens
 * when Python has put the socket in non-blocking mode (timeout > 0).
 */
static int
wait_readable(int fd)
{
    struct pollfd pfd = { fd, POLLIN, 0 };
    return (poll(&pfd, 1, SOCK_TIMEOUT_MS) > 0);
}

static int
wait_writable(int fd)
{
    struct pollfd pfd = { fd, POLLOUT, 0 };
    return (poll(&pfd, 1, SOCK_TIMEOUT_MS) > 0);
}

typedef struct { uint64_t bytes; uint64_t time_ns; } Sample;

static uint64_t
monotonic_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* Build a Python list from a C samples array. */
static PyObject *
build_samples(const Sample *smp, int n)
{
    PyObject *lst = PyList_New(n);
    if (!lst) return NULL;
    for (int i = 0; i < n; i++) {
        PyObject *t = Py_BuildValue("(KK)", smp[i].bytes, smp[i].time_ns);
        if (!t) { Py_DECREF(lst); return NULL; }
        PyList_SET_ITEM(lst, i, t);
    }
    return lst;
}

/*
 * download_loop(fd, initial_bytes, chunk_size, read_block, sample_ns)
 *
 * Receives RMBT data chunks from fd until the terminal byte (0xFF) ends a
 * chunk.  initial_bytes is any data already buffered by Python (conn._buf).
 * Returns (total_bytes, samples, leftover_bytes).
 */
static PyObject *
py_download_loop(PyObject *self, PyObject *args)
{
    int                fd;
    const char        *init_ptr;
    Py_ssize_t         init_len;
    Py_ssize_t         chunk_size, read_block;
    unsigned long long sample_ns;

    if (!PyArg_ParseTuple(args, "iy#nnK",
                          &fd, &init_ptr, &init_len,
                          &chunk_size, &read_block, &sample_ns))
        return NULL;

    unsigned char *buf = malloc((size_t)read_block);
    if (!buf) return PyErr_NoMemory();

    /* 1024 slots: ample for any test duration at 25 samples/s */
    int     max_smp  = 1024;
    Sample *smp      = malloc((size_t)max_smp * sizeof(Sample));
    if (!smp) { free(buf); return PyErr_NoMemory(); }

    int        nsmp     = 0;
    uint64_t   total    = 0;
    int        err_flag = 0;
    Py_ssize_t in_chunk = chunk_size;
    Py_ssize_t init_off = 0;   /* bytes consumed from init_ptr */

    /* origin anchor */
    smp[nsmp].bytes   = 0;
    smp[nsmp].time_ns = 0;
    nsmp++;

    uint64_t t0 = 0, last_sample = 0;

    Py_BEGIN_ALLOW_THREADS

    t0 = last_sample = monotonic_ns();

    while (!err_flag) {
        Py_ssize_t want = (in_chunk < read_block) ? in_chunk : read_block;
        Py_ssize_t got  = 0;

        /* Drain the initial Python buffer first. */
        if (init_off < init_len) {
            Py_ssize_t take = init_len - init_off;
            if (take > want) take = want;
            memcpy(buf, init_ptr + init_off, (size_t)take);
            init_off += take;
            got       = take;
        }

        /* Fill remainder from socket, handling non-blocking fd (EAGAIN). */
        while (got < want) {
            ssize_t n = recv(fd, buf + got, (size_t)(want - got), 0);
            if (n > 0) {
                got += (Py_ssize_t)n;
            } else if (n == 0) {
                err_flag = 1; break;   /* connection closed */
            } else if (errno == EINTR) {
                continue;
            } else if ((errno == EAGAIN || errno == EWOULDBLOCK)
                       && wait_readable(fd)) {
                continue;
            } else {
                err_flag = 1; break;
            }
        }
        if (err_flag) break;

        total    += (uint64_t)got;
        in_chunk -= got;

        uint64_t now = monotonic_ns();
        if ((now - last_sample) >= sample_ns && nsmp < max_smp - 1) {
            smp[nsmp].bytes   = total;
            smp[nsmp].time_ns = now - t0;
            nsmp++;
            last_sample = now;
        }

        if (in_chunk == 0) {
            if (buf[got - 1] == 0xFF) break;   /* terminal chunk received */
            in_chunk = chunk_size;
        }
    }

    Py_END_ALLOW_THREADS

    if (err_flag) {
        free(buf); free(smp);
        PyErr_SetString(PyExc_ConnectionError, "recv failed in download_loop");
        return NULL;
    }
    free(buf);

    PyObject *pysmp = build_samples(smp, nsmp);
    free(smp);
    if (!pysmp) return NULL;

    /* Return any unconsumed bytes from the initial buffer (normally empty). */
    PyObject *leftover = PyBytes_FromStringAndSize(
        init_ptr + init_off, init_len - init_off);
    if (!leftover) { Py_DECREF(pysmp); return NULL; }

    PyObject *result = Py_BuildValue("(KOO)",
        (unsigned long long)total, pysmp, leftover);
    Py_DECREF(pysmp);
    Py_DECREF(leftover);
    return result;
}

/*
 * upload_loop(fd, chunk_size, write_block, duration_ns, sample_ns)
 *
 * Sends RMBT data chunks until duration_ns has elapsed, then sends one
 * terminal chunk (last byte 0xFF).  Returns (total_bytes, samples).
 */
static PyObject *
py_upload_loop(PyObject *self, PyObject *args)
{
    int                fd;
    Py_ssize_t         chunk_size, write_block;
    unsigned long long duration_ns, sample_ns;

    if (!PyArg_ParseTuple(args, "innKK",
                          &fd, &chunk_size, &write_block,
                          &duration_ns, &sample_ns))
        return NULL;

    unsigned char *chunk = malloc((size_t)chunk_size);
    if (!chunk) return PyErr_NoMemory();

    /* Fill with i % 256 pattern (same as C client). */
    for (Py_ssize_t i = 0; i < chunk_size; i++)
        chunk[i] = (unsigned char)(i & 0xFF);

    int     max_smp = 1024;
    Sample *smp     = malloc((size_t)max_smp * sizeof(Sample));
    if (!smp) { free(chunk); return PyErr_NoMemory(); }

    int      nsmp     = 0;
    uint64_t total    = 0;
    int      err_flag = 0;

    /* origin anchor */
    smp[nsmp].bytes   = 0;
    smp[nsmp].time_ns = 0;
    nsmp++;

    uint64_t t0 = 0, last_sample = 0, deadline = 0;

    Py_BEGIN_ALLOW_THREADS

    t0 = last_sample = monotonic_ns();
    deadline = t0 + duration_ns;

    for (;;) {
        int terminal = (monotonic_ns() >= deadline);
        chunk[chunk_size - 1] = terminal ? 0xFF : 0x00;

        Py_ssize_t sent = 0;
        while (sent < chunk_size && !err_flag) {
            Py_ssize_t want = chunk_size - sent;
            if (want > write_block) want = write_block;

            /* sendall for this block, handling non-blocking fd (EAGAIN). */
            Py_ssize_t off = 0;
            while (off < want) {
                ssize_t n = send(fd, chunk + sent + off,
                                 (size_t)(want - off), MSG_NOSIGNAL);
                if (n > 0) {
                    off += (Py_ssize_t)n;
                } else if (errno == EINTR) {
                    continue;
                } else if ((errno == EAGAIN || errno == EWOULDBLOCK)
                           && wait_writable(fd)) {
                    continue;
                } else {
                    err_flag = 1; break;
                }
            }
            sent  += off;
            total += (uint64_t)off;

            if (!terminal && !err_flag) {
                uint64_t now = monotonic_ns();
                if ((now - last_sample) >= sample_ns && nsmp < max_smp - 1) {
                    smp[nsmp].bytes   = total;
                    smp[nsmp].time_ns = now - t0;
                    nsmp++;
                    last_sample = now;
                }
            }
        }

        if (err_flag || terminal) break;
    }

    Py_END_ALLOW_THREADS

    free(chunk);

    if (err_flag) {
        free(smp);
        PyErr_SetString(PyExc_ConnectionError, "send failed in upload_loop");
        return NULL;
    }

    PyObject *pysmp = build_samples(smp, nsmp);
    free(smp);
    if (!pysmp) return NULL;

    PyObject *result = Py_BuildValue("(KO)", (unsigned long long)total, pysmp);
    Py_DECREF(pysmp);
    return result;
}

static PyMethodDef methods[] = {
    {"download_loop", py_download_loop, METH_VARARGS,
     "download_loop(fd, initial_bytes, chunk_size, read_block, sample_ns)"
     " -> (total_bytes, samples, leftover_bytes)"},
    {"upload_loop",   py_upload_loop,   METH_VARARGS,
     "upload_loop(fd, chunk_size, write_block, duration_ns, sample_ns)"
     " -> (total_bytes, samples)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "rmbt_loop", NULL, -1, methods
};

PyMODINIT_FUNC
PyInit_rmbt_loop(void)
{
    return PyModule_Create(&module);
}
