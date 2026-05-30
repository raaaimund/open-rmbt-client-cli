#pragma once
/*
 * macOS does not implement pthread barriers (they are an optional POSIX extension).
 * Emulate them here with a mutex + condition variable.
 * On Linux this file is a no-op.
 */
#ifdef __APPLE__
#include <pthread.h>

#ifndef PTHREAD_BARRIER_SERIAL_THREAD
#define PTHREAD_BARRIER_SERIAL_THREAD (-1)
#endif

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t  cond;
    int             count;
    int             threshold;
    int             generation;
} pthread_barrier_t;

typedef int pthread_barrierattr_t;

static inline int
pthread_barrier_init(pthread_barrier_t *b, const pthread_barrierattr_t *a, unsigned n)
{
    (void)a;
    b->count = 0; b->threshold = (int)n; b->generation = 0;
    pthread_mutex_init(&b->mutex, NULL);
    pthread_cond_init(&b->cond, NULL);
    return 0;
}

static inline int pthread_barrier_wait(pthread_barrier_t *b)
{
    pthread_mutex_lock(&b->mutex);
    int gen = b->generation;
    if (++b->count == b->threshold) {
        b->generation++;
        b->count = 0;
        pthread_cond_broadcast(&b->cond);
        pthread_mutex_unlock(&b->mutex);
        return PTHREAD_BARRIER_SERIAL_THREAD;
    }
    while (gen == b->generation)
        pthread_cond_wait(&b->cond, &b->mutex);
    pthread_mutex_unlock(&b->mutex);
    return 0;
}

static inline int pthread_barrier_destroy(pthread_barrier_t *b)
{
    pthread_mutex_destroy(&b->mutex);
    pthread_cond_destroy(&b->cond);
    return 0;
}
#endif /* __APPLE__ */
