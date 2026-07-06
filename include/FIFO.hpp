#ifndef FIFO_H
#define FIFO_H

#include <iostream>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <list>

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

#endif