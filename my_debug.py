'''
	sample debug tool.
	if arg for my_debug ( ,arg) is a python dict, it prints as pritty json, otherwise prints normally.

'''

class Config:
	log2file=False
	debug_file='debug.txt'

class _TestClass_:
	def __init__(self):
		pass

def sanity_process(arg):
	#import string
	#using str type gives some strage error in object-server code.
	'''for k in arg:
		if type(arg[k]) != str:
			arg[k] = str(arg[k])
	return arg'''
	return arg

'''
don't work for zerocloud. why ?
'''
def write2file( arg):
	
    try:
        f = open(Config.debug_file,'w')
        f.write(arg)
    except Exception as e:
        print '*'*30 + e

def write_and_print(arg):
	print arg
	if Config.log2file:
	    pass
	    #write2file(str(arg) + "\n")

def my_debug(beacon, arg):
        import json
	#import  string

	write_and_print( "Start:------------------{}----------------------".format(beacon))
	try:
		if type(arg) == dict: 			
        		d_arg = sanity_process(arg)
               		x = json.dumps(d_arg, sort_keys=True, indent=4, separators=(',', ': ')) # pass dict in json.dumps
			write_and_print(x)
		else:
			write_and_print (arg)
        except Exception as e:
                write_and_print (arg)
		write_and_print ("~~~~~~~{}~~~~~~~".format(e))
        write_and_print ("End:------------------{}----------------------".format(beacon))


class MyDebug:
	def __init__(self,beacon,arg):
		#my_debug(beacon,arg)
		self.beacon = beacon
		self.arg = arg

	def inspect(self):
		write_and_print( "Start:------------------{}----------------------".format(self.beacon))
		for x in dir(self.arg):
			try:
				if x in self.arg.__dict__:
					write_and_print("{}:{}".format(x,self.arg.__dict__[x]))
				elif x in self.arg.__class__.__dict__:
					write_and_print("{}:{}".format(x,self.arg.__class__.__dict__[x]))
			except Exception:
				pass
		
		write_and_print( "Start:------------------{}----------------------".format(self.beacon))
		

def test():
        MyDebug("dict equivalent json: ", {'a':'a','b':[10,20]}).inspect()
        MyDebug("non dict, non json", [20,30]).inspect()
        MyDebug("string", "string").inspect()
        MyDebug("object dict", {'a':100, 'b':_TestClass_.__dict__}).inspect()
	#print sanity_process({'a':TestClass.__dict__,'b':100})
if __name__== "__main__":
        test()
