#ifndef FIFO_H
#define FIFO_H

#include <iostream>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <list>
#include <atomic>
#include <chrono>

// If define template class type, this need to be used in same file

template<class T>
class MutexFIFO{
    public:
        void push(T);
        bool pop(T&);
        int size();
        void lock();
        void unlock();
        std::list<T> queue;  // may make this private if want max protection
    private:
    std::recursive_mutex mtx;
};

template<class T>
void MutexFIFO<T>::push(T entry){  // entry: adds entry to the end of the queue 
    std::lock_guard<std::recursive_mutex> scoped_lock(mtx);
    queue.push_back(entry);
}

template<class T>
int MutexFIFO<T>::size(void){
    std::lock_guard<std::recursive_mutex> scoped_lock(mtx);
    return queue.size();
}

template<class T>
bool MutexFIFO<T>::pop(T& entry){
    std::lock_guard<std::recursive_mutex> scoped_lock(mtx);
    bool is_not_empty = not queue.empty();
    if (is_not_empty){
        entry = queue.front();
        queue.pop_front();
    }
    return is_not_empty;
}

template<class T>
void MutexFIFO<T>::lock(void){
    mtx.lock();
}

template<class T>
void MutexFIFO<T>::unlock(void){
    mtx.unlock();
}

// ─────────────────────────────────────────────────────────────
//  DrainGate — bounds a "drain-on-exit" worker loop so shutdown
//  always terminates. Replaces the pattern
//      while (!stop_sign || fifo.size() > 0) { ... }
//  which HANGS if an upstream stage keeps feeding `fifo` after stop
//  (the guard never sees size()==0). Usage:
//      DrainGate gate;
//      while (gate.keep_going(stop_sign, fifo)) { ... }
//  Before stop: always continue. After stop flips true: keep draining
//  pending items, but only until `grace` elapses — then force-exit even
//  if the fifo is still non-empty, so a single Ctrl-C reliably unwinds
//  the whole pipeline (each stage exits within `grace` of the flag).
class DrainGate {
public:
    explicit DrainGate(std::chrono::milliseconds grace = std::chrono::milliseconds(300))
        : grace_(grace) {}

    template <class StopT, class FifoT>
    bool keep_going(const StopT& stop_sign, FifoT& fifo) {
        if (!stop_sign) return true;                 // not stopping: run normally
        if (!armed_) {                               // first tick after stop: arm deadline
            armed_    = true;
            deadline_ = std::chrono::steady_clock::now() + grace_;
        }
        if (std::chrono::steady_clock::now() >= deadline_) return false;  // grace up: force exit
        return fifo.size() > 0;                       // else drain what's left, then exit
    }

private:
    std::chrono::milliseconds                      grace_;
    bool                                           armed_ = false;
    std::chrono::steady_clock::time_point          deadline_{};
};

#endif