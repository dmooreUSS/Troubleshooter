# Network Troubleshooter v1 Created By Daniel Moore

===========================================================================================================================

Network Troubleshooter v1 is a simple troubleshooting that walks users through troubleshooting common issues T1s will work on the Network side.  Follow the instructions below.  If you have questions, concerns or feedback on the troubleshooter feel free to ping me.

===========================================================================================================================

### **Download Instructions**

===========================================================================================================================

1. Go to  https://github.com/dmooreUSS/Troubleshooter
2. Click green button that says Code
3. Go to and click download ZIP
4. Wait for download
5. Go to Downloads folder and right click on zip folder "Troubleshooter-main", Choose Extract All
6. Go to uncompressed file, navigate folders until you see "NetworkTroubleshooter.exe"
7. If Windows pop up go to more info (or click the down arrow) and choose Run anyway

===========================================================================================================================

### **Use a Workflow**

===========================================================================================================================

1. Type in the number from workflow menu that you are interested in using:

   1. ADVA: *Workflow for ADVA navigation and troubleshooting(when ADVA is reachable)*
   2. CFS: *Workflow for troubleshooting and resolving CFS cases*
   3. Outage-AGG/PEC: *Workflow for Outages and circuit checking for AGGs/PECS*
   4. Outage-Switch: *Workflow for Outages and circuit checking for Switches(MSWs)*
   5. Packet Loss/Bandwidth: *Workflow for checking Packet Loss and potential causes.*
   6. RFO: *Workflow to find RFO for a specific time frame*
   7. *arp ca*che: *Workflow to view and clear the arp cache*

2\. Enter the ticket number

3\. Also, add the requested BVI, Interface or IP Address when prompted.
	**\*Note: BVI has to be added to input i.e. Please enter the BVI: BVI 22207**

2. Read and follow the carefully instructions shown for each step.
3. Copy and paste the commands that shown in each step in the appropriate system
4. If there is a question associated with step, please read carefully with appropriate response (Y for Yes and N for No)
5. Fill out the findings section with details of what you discovered for that step.

&#x09;**Example:** *Enter your findings for this step: BVI is up up and Last input is 00:32:00, Output is 00:00:00*



8\. After completing  all the steps you be prompted for a *Conclusion* this is where you type a summary of what you discovered during the workflow.  For example, ADVA is unreachable and there is no input traffic.



9\. Once at the end, a summary will be displayed that details each of your findings and answer to questions.  Copy and paste that summary into the case as your notes.

===========================================================================================================================



##### **\*Note: For program to work properly, all files need to stay in same folder.  Specifically, the \_internal, NetworkTroubleshooter.exe, Network\_yes\_no.db, and troubleshooting\_summaries folder**

##### 

