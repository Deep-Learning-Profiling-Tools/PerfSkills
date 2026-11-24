// Concurrent activity collection only. No replay, forced synchronization, or kernel serialization.
#include <cupti.h>
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cxxabi.h>
#include <fstream>
#include <map>
#include <mutex>
#include <string>
#include <utility>
#include <vector>
#include <unistd.h>

#ifndef PW_CUPTI_KERNEL_RECORD
#error "The collector builder must bind the selected CUPTI SDK kernel record type"
#endif

namespace {
struct Interval { uint64_t start, end; };
struct Marker { uint32_t id, flags; uint64_t time; std::string name, domain; };
struct Kernel { uint64_t calls=0, ns=0; int grid[3]={}, block[3]={}; uint32_t registers=0; std::vector<Interval> intervals; };
struct State {
  std::mutex mutex;
  std::map<std::string, Kernel> kernels;
  uint64_t copies=0, copy_bytes=0, copy_ns=0, dropped=0, errors=0;
  std::vector<Interval> copy_intervals;
  std::vector<Marker> markers;
  std::map<std::pair<std::string, uint32_t>, bool> open_markers;
  std::map<std::pair<std::string, uint32_t>, Marker> early_ends;
  uint64_t retained=0;
  bool truncated=false;
};
State &state() { static State *s = new State; return *s; }
bool phase_requested() { return std::getenv("PW_CUPTI_ROI") != nullptr; }
void retain(std::vector<Interval> &items, uint64_t start, uint64_t end) {
  if (!phase_requested()) return;
  if (++state().retained <= 1000000) items.push_back({start,end});
  else state().truncated=true;
}
void marker(const CUpti_ActivityMarker2 &input) {
  const char *roi = std::getenv("PW_CUPTI_ROI");
  if (!roi) return;
  static const std::string excluded = std::string(roi) + "-excluded";
  auto &s = state();
  Marker event{input.id, static_cast<uint32_t>(input.flags), input.timestamp,
               input.name ? input.name : "", input.domain ? input.domain : ""};
  auto key = std::make_pair(event.domain, event.id);
  auto save = [&s](const Marker &value) {
    if (s.markers.size() < 100000) s.markers.push_back(value);
    else s.truncated = true;
  };
  if (event.flags & CUPTI_ACTIVITY_FLAG_MARKER_START) {
    bool relevant = event.name == roi || event.name == excluded;
    if (relevant) save(event);
    auto end = s.early_ends.find(key);
    if (end != s.early_ends.end()) {
      if (relevant) save(end->second);
      s.early_ends.erase(end);
    } else if (s.open_markers.size() < 100000) {
      s.open_markers.emplace(key, relevant);
    } else s.truncated = true;
  } else if (event.flags & CUPTI_ACTIVITY_FLAG_MARKER_END) {
    auto start = s.open_markers.find(key);
    if (start != s.open_markers.end()) {
      if (start->second) save(event);
      s.open_markers.erase(start);
    } else if (s.early_ends.size() < 100000) {
      s.early_ends.emplace(key, event);
    } else s.truncated = true;
  }
}
void error(CUptiResult result) { if (result != CUPTI_SUCCESS) { std::lock_guard<std::mutex> guard(state().mutex); ++state().errors; } }
std::string quote(const std::string &s) {
  std::string out="\"";
  for (unsigned char c : s) {
    if (c=='\\' || c=='\"') { out+='\\'; out+=c; }
    else if (c<32) { char b[7]; std::snprintf(b,sizeof(b),"\\u%04x",c); out+=b; }
    else out+=c;
  }
  return out+'\"';
}
void CUPTIAPI request(uint8_t **buffer, size_t *size, size_t *records) {
  *size=1<<20; *records=0;
  if (posix_memalign(reinterpret_cast<void **>(buffer), 64, *size)) { *buffer=nullptr; *size=0; }
}
void CUPTIAPI complete(CUcontext context, uint32_t stream, uint8_t *buffer, size_t, size_t valid) {
  CUpti_Activity *record=nullptr;
  CUptiResult result;
  while ((result=cuptiActivityGetNextRecord(buffer,valid,&record))==CUPTI_SUCCESS) {
    std::lock_guard<std::mutex> guard(state().mutex);
    if (record->kind==CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL || record->kind==CUPTI_ACTIVITY_KIND_KERNEL) {
      auto *k=reinterpret_cast<PW_CUPTI_KERNEL_RECORD *>(record);
      if (!k->start || k->end<k->start) { ++state().errors; continue; }
      auto &value=state().kernels[k->name ? k->name : "unknown"];
      if (!value.calls) {
        value.grid[0]=k->gridX; value.grid[1]=k->gridY; value.grid[2]=k->gridZ;
        value.block[0]=k->blockX; value.block[1]=k->blockY; value.block[2]=k->blockZ;
        value.registers=k->registersPerThread;
      }
      ++value.calls; value.ns+=k->end-k->start;
      retain(value.intervals,k->start,k->end);
    } else if (record->kind==CUPTI_ACTIVITY_KIND_MEMCPY) {
      auto *m=reinterpret_cast<CUpti_ActivityMemcpy6 *>(record);
      if (!m->start || m->end<m->start) { ++state().errors; continue; }
      ++state().copies; state().copy_bytes+=m->bytes; state().copy_ns+=m->end-m->start;
      retain(state().copy_intervals,m->start,m->end);
    } else if (record->kind==CUPTI_ACTIVITY_KIND_MARKER) {
      marker(*reinterpret_cast<CUpti_ActivityMarker2 *>(record));
    }
  }
  if (result!=CUPTI_ERROR_MAX_LIMIT_REACHED) error(result);
  size_t dropped=0;
  error(cuptiActivityGetNumDroppedRecords(context,stream,&dropped));
  { std::lock_guard<std::mutex> guard(state().mutex); state().dropped+=dropped; }
  free(buffer);
}
void finish() {
  error(cuptiActivityFlushAll(CUPTI_ACTIVITY_FLAG_FLUSH_FORCED));
  auto *base=std::getenv("PW_CUPTI_OUTPUT");
  if (!base) return;
  std::ofstream out(std::string(base)+"."+std::to_string(getpid())+".json");
  std::lock_guard<std::mutex> guard(state().mutex);
  std::vector<std::pair<std::string,Kernel>> kernels(state().kernels.begin(),state().kernels.end());
  std::sort(kernels.begin(),kernels.end(),[](const auto &a,const auto &b){return a.second.ns>b.second.ns;});
  uint64_t total=0,calls=0;
  for (auto &k : kernels) { total+=k.second.ns; calls+=k.second.calls; }
  uint32_t version=0; cuptiGetVersion(&version);
  out<<"{\"protocol\":\"profileweaver.cupti-activity/v1\",\"cupti_version\":"<<version
     <<",\"pid\":"<<getpid()<<",\"kernel_total_ns\":"<<total<<",\"launches\":"<<calls
     <<",\"copy_calls\":"<<state().copies<<",\"copy_bytes\":"<<state().copy_bytes
     <<",\"copy_total_ns\":"<<state().copy_ns<<",\"dropped\":"<<state().dropped
     <<",\"errors\":"<<state().errors<<",\"kernels\":[";
  for (size_t i=0;i<std::min<size_t>(kernels.size(),phase_requested()?kernels.size():16);++i) {
    if(i) out<<',';
    int status=0;
    char *name=abi::__cxa_demangle(kernels[i].first.c_str(),nullptr,nullptr,&status);
    out<<"{\"name\":"<<quote(status==0 && name ? name : kernels[i].first)
       <<",\"calls\":"<<kernels[i].second.calls<<",\"total_ns\":"<<kernels[i].second.ns
       <<",\"first_grid\":["<<kernels[i].second.grid[0]<<','<<kernels[i].second.grid[1]<<','<<kernels[i].second.grid[2]<<']'
       <<",\"first_block\":["<<kernels[i].second.block[0]<<','<<kernels[i].second.block[1]<<','<<kernels[i].second.block[2]<<']'
       <<",\"registers_per_thread\":"<<kernels[i].second.registers;
    if (phase_requested()) {
      out<<",\"intervals_ns\":[";
      for (size_t j=0;j<kernels[i].second.intervals.size();++j) {
        if(j) out<<',';
        auto &p=kernels[i].second.intervals[j]; out<<'['<<p.start<<','<<p.end<<']';
      }
      out<<']';
    }
    out<<'}';
    free(name);
  }
  out<<"],\"phase_records_truncated\":"<<(state().truncated?"true":"false")<<",\"markers\":[";
  for(size_t i=0;i<state().markers.size();++i) {
    if(i) out<<',';
    auto &m=state().markers[i];
    out<<"{\"id\":"<<m.id<<",\"flags\":"<<m.flags<<",\"timestamp\":"<<m.time
       <<",\"name\":"<<quote(m.name)<<",\"domain\":"<<quote(m.domain)<<'}';
  }
  out<<"],\"copy_intervals_ns\":[";
  for(size_t i=0;i<state().copy_intervals.size();++i) {
    if(i) out<<','; auto &p=state().copy_intervals[i]; out<<'['<<p.start<<','<<p.end<<']';
  }
  out<<"]}\n";
}
}
extern "C" int InitializeInjection() {
  static bool initialized=false;
  if (initialized) return 1;
  initialized=true;
  error(cuptiActivityRegisterCallbacks(request,complete));
  error(cuptiActivityEnable(CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL));
  error(cuptiActivityEnable(CUPTI_ACTIVITY_KIND_MEMCPY));
  if (phase_requested()) error(cuptiActivityEnable(CUPTI_ACTIVITY_KIND_MARKER));
  std::atexit(finish);
  return 1;
}
